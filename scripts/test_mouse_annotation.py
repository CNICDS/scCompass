"""Mouse annotation regression tests without the human ML model dependencies.

Run: python -m unittest discover -s scripts -p 'test_mouse_annotation.py' -v
"""

import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules import annotation as ANNOTATION


class MouseImportTests(unittest.TestCase):
    def test_public_entry_does_not_import_other_pipeline_dependencies(self):
        code = '''
import sys
class RejectMLImports:
    def find_spec(self, fullname, *args):
        if fullname.split(".")[0] in {"scanpy", "anndata", "scimilarity", "torch", "datasets"}:
            raise AssertionError("Mouse annotation imported " + fullname)
sys.meta_path.insert(0, RejectMLImports())
from modules import AnnotationMouse
AnnotationMouse(".")
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_main_mouse_entry_reaches_r_without_ml_dependencies(self):
        import main
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.csv"
            source.write_text(",Actb\na,1\nb,2\nc,3\n")
            def run_r(command, **kwargs):
                result = Path(command[3]) / "sample_cell_type.csv"
                result.write_text(',0\n0,T cell\n1,B cell\n2,Unknown\n')
            with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=run_r) as run:
                main.data_annotation_pipeline(str(source), "mouse", directory, str(ROOT), "unused")
            run.assert_called_once()


class MouseAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sccompass-mouse-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.input = self.directory / "sample.csv"
        self.input.write_text(",Actb,Cd3d\ncell_a,1,2\ncell_b,3,4\n")
        self.output = self.directory / "output"
        self.sample_dir = self.output / "mouse" / "sample"
        self.result = self.sample_dir / "sample_cell_type.csv"
        self.lock = self.output / "mouse" / "sample.tmp"
        self.logs = self.sample_dir / "logs.txt"
        self.stdout = io.StringIO()
        self.redirect = redirect_stdout(self.stdout)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.annotator = ANNOTATION.AnnotationMouse(str(ROOT))

    def run_annotation(self, input_path=None):
        self.annotator(str(input_path or self.input), output_dir=str(self.output))

    def write_result(self, *_args, **_kwargs):
        self.result.write_text(',0\n0,T cell\n1,Unknown\n')
        return subprocess.CompletedProcess(["Rscript"], 0)

    def test_success_writes_log_and_removes_lock(self):
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result) as run:
            self.run_annotation()
        run.assert_called_once_with(
            ["Rscript", str(ROOT / "scripts" / "mouse_annotation.R"),
             str(self.input), str(self.sample_dir), "mouse"],
            check=True,
        )
        self.assertEqual(self.logs.read_text(), "success\n")
        self.assertFalse(self.lock.exists())
        self.assertTrue(self.result.is_file())

    def test_failed_run_can_be_retried_without_false_success(self):
        with mock.patch.object(
            ANNOTATION.subprocess, "run",
            side_effect=subprocess.CalledProcessError(1, ["Rscript"]),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_annotation()
        self.assertFalse(self.lock.exists())
        self.assertNotIn("success", self.logs.read_text() if self.logs.exists() else "")
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result) as run:
            self.run_annotation()
        run.assert_called_once()
        self.assertEqual(self.logs.read_text().count("success"), 1)

    def test_preexisting_empty_directory_is_retried(self):
        self.sample_dir.mkdir(parents=True)
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result) as run:
            self.run_annotation()
        run.assert_called_once()

    def test_active_lock_is_not_removed(self):
        self.sample_dir.mkdir(parents=True)
        self.lock.write_text("another worker")
        with mock.patch.object(ANNOTATION.subprocess, "run") as run:
            self.run_annotation()
        run.assert_not_called()
        self.assertEqual(self.lock.read_text(), "another worker")

    def test_completed_result_is_skipped(self):
        self.sample_dir.mkdir(parents=True)
        self.write_result()
        with mock.patch.object(ANNOTATION.subprocess, "run") as run:
            self.run_annotation()
        run.assert_not_called()
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.logs.exists())

    def test_exit_zero_without_result_is_failure(self):
        with mock.patch.object(ANNOTATION.subprocess, "run", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "result"):
                self.run_annotation()
        self.assertFalse(self.lock.exists())
        self.assertNotIn("success", self.logs.read_text() if self.logs.exists() else "")

    def test_invalid_existing_outputs_are_retried(self):
        for content in ("", ",0\n", ",wrong\n0,T cell\n", ",0\n1,T cell\n",
                        ",0\n0,NA\n", ",0\n0,   \n", ",0\n0,T cell\n",
                        ",0\n0,T cell\n1,B cell\n2,Unknown\n"):
            with self.subTest(content=content):
                self.sample_dir.mkdir(parents=True, exist_ok=True)
                self.result.write_text(content)
                with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result) as run:
                    self.run_annotation()
                run.assert_called_once()

    def test_invalid_new_output_is_failure(self):
        def write_invalid_result(*_args, **_kwargs):
            self.result.write_text(",0\n0,NA\n")
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=write_invalid_result):
            with self.assertRaisesRegex(RuntimeError, "result"):
                self.run_annotation()
        self.assertFalse(self.lock.exists())
        self.assertNotIn("success", self.logs.read_text() if self.logs.exists() else "")

    def test_truncated_new_output_is_failure(self):
        def write_short_result(*_args, **_kwargs):
            self.result.write_text(",0\n0,T cell\n")
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=write_short_result):
            with self.assertRaisesRegex(RuntimeError, "result"):
                self.run_annotation()
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.logs.exists())

    def test_path_objects_are_supported_for_file_and_directory(self):
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result):
            self.annotator(self.input, output_dir=self.output)
        sample = self.directory / "sample"
        sample.mkdir()
        self.input.rename(sample / "sample.csv")
        with mock.patch.object(ANNOTATION.subprocess, "run") as run:
            self.annotator(sample, output_dir=self.output)
        run.assert_not_called()

    def test_csv_quoted_commas_and_newlines_keep_record_count(self):
        self.input.write_text(',Actb\n"cell,a",1\n"cell\nb",2\n')
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result):
            self.run_annotation()
        self.assertTrue(self.result.is_file())

    def test_non_mouse_species_is_rejected_before_cached_results(self):
        with self.assertRaisesRegex(ValueError, "only mouse"):
            self.annotator(self.input, specie="rat", output_dir=self.output)

    def test_missing_rscript_does_not_poison_retry(self):
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=FileNotFoundError("Rscript")):
            with self.assertRaises(FileNotFoundError):
                self.run_annotation()
        self.assertFalse(self.lock.exists())
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result) as run:
            self.run_annotation()
        run.assert_called_once()

    def test_missing_input_is_an_error(self):
        with mock.patch.object(ANNOTATION.subprocess, "run") as run:
            with self.assertRaises(FileNotFoundError):
                self.run_annotation(self.directory / "missing.csv")
        run.assert_not_called()

    def test_sample_directory_input_remains_supported(self):
        sample = self.directory / "sample"
        sample.mkdir()
        self.input.rename(sample / "sample.csv")
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=self.write_result) as run:
            self.run_annotation(sample)
        self.assertEqual(run.call_args.args[0][2], str(sample / "sample.csv"))

    def test_concurrent_worker_cannot_steal_lock(self):
        def run_while_locked(*args, **kwargs):
            self.assertTrue(self.lock.is_file())
            self.run_annotation()
            self.assertTrue(self.lock.is_file())
            return self.write_result(*args, **kwargs)
        with mock.patch.object(ANNOTATION.subprocess, "run", side_effect=run_while_locked) as run:
            self.run_annotation()
        run.assert_called_once()
        self.assertFalse(self.lock.exists())


@unittest.skipUnless(shutil.which("bash") and shutil.which("xargs"), "bash and xargs required")
class MouseBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sccompass-mouse-batch-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.input_dir = self.directory / "input with spaces"
        self.input_dir.mkdir()
        self.input = self.input_dir / "sample.csv"
        self.input.write_text(",Actb\ncell_a,1\n")
        self.output = self.directory / "output with spaces"
        self.result_dir = self.output / "mouse" / "sample"
        self.result = self.result_dir / "sample_cell_type.csv"
        self.lock = self.output / "mouse" / "sample.tmp"
        self.bin_dir = self.directory / "bin"
        self.bin_dir.mkdir()
        self.fake_r = self.bin_dir / "Rscript"
        self.env = dict(os.environ, PATH=str(self.bin_dir) + os.pathsep + os.environ["PATH"],
                        PYTHON=sys.executable)

    def run_batch(self, body):
        self.fake_r.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        self.fake_r.chmod(0o755)
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "annotation_mouse.sh"),
             str(self.input_dir), str(self.output), "mouse", "2"],
            env=self.env, capture_output=True, text=True, timeout=15,
        )

    def test_failed_r_process_is_nonzero_and_retry_succeeds(self):
        failed = self.run_batch("exit 7")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertNotIn("All mouse annotation tasks completed", failed.stdout)
        self.assertFalse(self.lock.exists())
        success = self.run_batch('printf ",0\\n0,Unknown\\n" > "$3/sample_cell_type.csv"')
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        self.assertTrue(self.result.is_file())
        self.assertFalse(self.lock.exists())

    def test_exit_zero_without_result_is_nonzero(self):
        result = self.run_batch("exit 0")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.lock.exists())

    def test_active_lock_is_preserved(self):
        self.result_dir.mkdir(parents=True)
        self.lock.write_text("another worker")
        result = self.run_batch("exit 9")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.lock.read_text(), "another worker")

    def test_completed_output_is_skipped(self):
        self.result_dir.mkdir(parents=True)
        self.result.write_text(",0\n0,Unknown\n")
        result = self.run_batch("exit 9")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.lock.exists())

    def test_empty_output_is_retried(self):
        self.result_dir.mkdir(parents=True)
        self.result.touch()
        result = self.run_batch('printf ",0\\n0,Unknown\\n" > "$3/sample_cell_type.csv"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(self.result.stat().st_size, 0)

    def test_invalid_nonempty_results_are_retried(self):
        for content in (",0\n", ",wrong\n0,T cell\n", ",0\n0,NA\n",
                        ",0\n1,T cell\n", ",0\n0,T cell\n1,B cell\n"):
            with self.subTest(content=content):
                self.result_dir.mkdir(parents=True, exist_ok=True)
                self.result.write_text(content)
                result = self.run_batch('printf ",0\\n0,Unknown\\n" > "$3/sample_cell_type.csv"')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.result.read_text(), ",0\n0,Unknown\n")

    def test_short_new_result_is_failure(self):
        self.input.write_text(",Actb\na,1\nb,2\n")
        result = self.run_batch('printf ",0\\n0,T cell\\n" > "$3/sample_cell_type.csv"')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.lock.exists())

    def test_appledouble_metadata_is_not_annotated(self):
        (self.input_dir / "._sample.csv").write_bytes(b"\x00\x05\x16\x07")
        result = self.run_batch('printf ",0\\n0,Unknown\\n" > "$3/sample_cell_type.csv"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("._sample.csv", result.stdout)
        self.assertFalse((self.output / "mouse" / "._sample").exists())

    def test_empty_input_directory_is_a_noop(self):
        self.input.unlink()
        result = self.run_batch("exit 9")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_batch_and_python_share_result_validation(self):
        self.result_dir.mkdir(parents=True)
        cases = (',0\n0,"T cell, activated"\n', ',0\n0,"T cell\nactivated"\n',
                 ',0\n0,"T cell\n', ',0\n0,T cell,extra\n', ',0\n0,NaN\n')
        for content in cases:
            with self.subTest(content=content):
                self.result.write_text(content)
                cli = subprocess.run(
                    [sys.executable, str(ROOT / "modules" / "annotation_result.py"),
                     str(self.result), str(self.input)], capture_output=True, timeout=15,
                )
                self.assertEqual(cli.returncode == 0, ANNOTATION.AnnotationMouse._valid_result(self.result, 1))


class MouseRTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("Rscript"), "Rscript is not installed")
    def test_r_regressions(self):
        result = subprocess.run(
            ["Rscript", str(ROOT / "scripts" / "test_mouse_annotation.R")],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
