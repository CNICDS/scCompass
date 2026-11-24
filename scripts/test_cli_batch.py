"""Batch failure handling must preserve fail-fast defaults and partial exit status."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("sccompass_main", Path(__file__).resolve().parents[1] / "main.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class BatchFailureTests(unittest.TestCase):
    def test_default_stops_at_failure(self):
        visited = []

        def process(data):
            visited.append(data)
            raise ValueError("invalid matrix")

        with self.assertRaisesRegex(ValueError, "invalid matrix"):
            cli._run_samples(process, ["bad", "after"], step="filter")
        self.assertEqual(visited, ["bad"])

    def test_continue_records_failure_and_processes_remaining_samples(self):
        visited = []

        def process(data, specie):
            self.assertEqual(specie, "human")
            visited.append(data)
            if data == "bad":
                raise ValueError("invalid matrix")

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "errors.jsonl"
            failures = cli._run_samples(process, ["good", "bad", "after"], step="filter",
                                        specie="human", continue_on_error=True, error_log=log)
            record = json.loads(log.read_text())
        self.assertEqual(failures, 1)
        self.assertEqual(visited, ["good", "bad", "after"])
        self.assertEqual((record["file"], record["step"], record["exception"]), ("bad", "filter", "ValueError"))

    def test_partial_cli_run_has_nonzero_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            arguments = ["main.py", "--species", "human", "--steps", "filter", "--input-pattern", "*.csv",
                         "--continue-on-error", "--error-log", str(Path(tmp) / "logs/errors.jsonl")]
            with patch("sys.argv", arguments), patch.object(cli, "data_filtering_pipeline", return_value=1) as operation:
                with self.assertRaises(SystemExit) as result:
                    cli.main()
                self.assertEqual(result.exception.code, 2)
                self.assertTrue(operation.call_args.kwargs["continue_on_error"])


if __name__ == "__main__":
    unittest.main()
