"""CLI regression tests for the per-sample count and metadata round trip."""

import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import anndata as ad
import numpy as np
from scipy import sparse


PROJECT = Path(__file__).resolve().parents[1]


class H5adExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.put("mapping/human_mapping.txt", "ENSG1\nENSG2\nENSG2\n")

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *options):
        return subprocess.run(
            [sys.executable, str(PROJECT / "main.py"), "--species", "human", "--steps", "h5ad",
             "--filter-output", str(self.root / "filtered"), "--map-output", str(self.root / "mapping"),
             "--annotate-output", str(self.root / "annotated"), "--h5ad-output", str(self.root / "h5ad"), *options],
            cwd=PROJECT, capture_output=True, text=True,
        )

    def test_exports_one_raw_count_file_per_sample(self):
        self.put("filtered/human/GSM1/GSM1.csv", ',Gene\n"0001",1\n"0001",2\n')
        self.put("filtered/human/GSM2/GSM2.csv", ",Gene\n002,3\n")
        self.put("filtered/human/GSM3/GSM3.csv", ",Gene\n")
        self.put("mapping/human/GSM1/GSM1.csv", "0,8589934592,2\n0,0,0\n")
        self.put("mapping/human/GSM2/GSM2.csv", "4,0,1\n")
        self.put("annotated/human/GSM1/GSM1_cell_type.csv", ",0\n0,T cell\n1,B cell\n")
        metadata = self.root / "metadata.csv.gz"
        with gzip.open(metadata, "wt", encoding="utf-8-sig") as stream:
            stream.write("SourceID,Tissue,SampleName\nGSM1,Brain,Example\n")
        result = self.run_cli("--h5ad-metadata", str(metadata))
        self.assertEqual(result.returncode, 0, result.stderr)

        files = sorted((self.root / "h5ad/human").iterdir())
        self.assertEqual([file.name for file in files], ["GSM1.h5ad", "GSM2.h5ad"])
        data = ad.read_h5ad(files[0])
        self.assertTrue(sparse.isspmatrix_csr(data.X))
        self.assertEqual(data.X.dtype, np.int64)
        np.testing.assert_array_equal(data.X.toarray(), [[0, 8589934592, 2], [0, 0, 0]])
        self.assertTrue(data.obs_names.is_unique and data.var_names.is_unique)
        self.assertEqual(data.obs["cell_barcode"].tolist(), ["0001", "0001"])
        self.assertEqual(data.obs["cell_type"].tolist(), ["T cell", "B cell"])
        self.assertEqual(data.obs["tissue"].tolist(), ["Brain", "Brain"])
        self.assertEqual(data.var["gene_id"].tolist(), ["ENSG1", "ENSG2", "ENSG2"])
        self.assertEqual(data.var["mapping_column"].tolist(), [0, 1, 2])
        self.assertEqual(json.loads(data.uns["sample_metadata_json"])["SampleName"], "Example")
        missing = ad.read_h5ad(files[1])
        self.assertTrue(missing.obs["cell_type"].isna().all())
        self.assertTrue(missing.obs["tissue"].isna().all())
        self.assertFalse(any(path.name == "hf" or path.suffix == ".arrow" for path in self.root.rglob("*")))

    def test_bad_sample_preserves_existing_output_and_batch_continues(self):
        self.put("filtered/human/GSM1/GSM1.csv", ",Gene\na,1\n")
        self.put("filtered/human/GSM2/GSM2.csv", ",Gene\nb,1\n")
        self.put("mapping/human/GSM1/GSM1.csv", "1,0\n")
        self.put("mapping/human/GSM2/GSM2.csv", "1,0,0\n")
        previous = self.put("h5ad/human/GSM1.h5ad", "previous output")
        errors = self.root / "errors.jsonl"
        result = self.run_cli("--continue-on-error", "--error-log", str(errors))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(previous.read_text(), "previous output")
        self.assertEqual(ad.read_h5ad(self.root / "h5ad/human/GSM2.h5ad").shape, (1, 3))
        error = json.loads(errors.read_text())
        self.assertEqual(error["step"], "h5ad")
        self.assertIn("column count", error["error"])
        self.assertEqual(len(list(previous.parent.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
