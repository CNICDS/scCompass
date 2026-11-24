"""Exercise the metadata CLI with offline snapshots and a temporary store."""

from contextlib import closing
import csv
import gzip
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
INTEGRATION = PROJECT / "integrations/metadata_v2"
DEPENDENCIES = ("yaml", "pydantic", "requests", "Bio", "xmltodict", "lxml")


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in DEPENDENCIES), "metadata dependencies are optional")
class MetadataV2Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_cli(self, *arguments):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MODEL", "NCBI", "OPENAI", "ANTHROPIC")) and key != "API_KEY"}
        return subprocess.run(
            [sys.executable, str(PROJECT / "main.py"), "metadata", *map(str, arguments)],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=30,
        )

    @staticmethod
    def raw_record(accession):
        return {
            "accession": accession, "source": "geo", "source_url": "https://example.test/geo",
            "fetched_at": "2026-11-01T00:00:00Z",
            "raw": {"Sample_geo_accession": accession, "Sample_title": "Example",
                    "Sample_organism_ch1": "Homo sapiens", "Sample_characteristics_ch1": ["tissue: brain"]},
        }

    def test_collect_from_saved_raw_and_resume_by_accession(self):
        accession = "GSM1234567"
        directory = self.root / "store"
        seed = """
from metadata_agent.store.raw import RawStore
from contextlib import closing
import json
import sys
with closing(RawStore("geo", sys.argv[1])) as store:
    store.upsert(json.loads(sys.argv[2]))
"""
        subprocess.run(
            [sys.executable, "-c", seed, str(directory), json.dumps(self.raw_record(accession))],
            cwd=self.root, env={**os.environ, "PYTHONPATH": str(INTEGRATION)}, check=True,
        )
        # The raw snapshot already has source evidence. Disable remote
        # publication lookup so this test can run with no network.
        config = self.root / "settings.private.yaml"
        config.write_text("""
extraction:
  tiers:
    T0:
      enabled: true
      fields: [Library, Assay, RNASource, RawFile, RawSize_GB, License]
    T1:
      enabled: false
      fields: [Pubmed ID, Citation, Journal, Year]
    T2:
      enabled: false
      fields: [AnnotationMethod, CellNumberRaw, CellNumberAfterQC, CellNumberAfterAnnotation, MedianUmi, DoubletsPerc]
""")
        manifest = self.root / "samples.txt"
        manifest.write_text(accession + "\n" + accession.lower() + "\n")
        command = ("collect", "--output", directory, "--accessions-file", manifest, "--skip-harvest",
                   "--no-enrich", "--config", config, "--workers", "1", "--resume")
        first = self.run_cli(*command)
        self.assertEqual(first.returncode, 0, first.stderr)
        with (directory / "metadata.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["SourceID"] for row in rows], [accession])
        self.assertEqual(rows[0]["Tissue"].lower(), "brain")
        second = self.run_cli(*command)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout.splitlines()[0])["stats"]["skipped"], 1)

    def test_review_retains_pending_rows_outside_csv_and_ignores_catalog_only(self):
        accession = "GSM1234567"
        snapshot = self.root / "human.jsonl.gz"
        directory = self.root / "review"
        with gzip.open(snapshot, "wt", encoding="utf-8") as stream:
            for row in (self.raw_record(accession),
                        {**self.raw_record("GSM1234568"), "metadata_evidence": "catalog_only"}):
                stream.write(json.dumps(row) + "\n")
        result = self.run_cli("review", "--input", snapshot, "--output", directory, "--all", "--max-llm-calls", "0")
        self.assertEqual(result.returncode, 2, result.stderr)
        with closing(sqlite3.connect(directory / "canonical.sqlite")) as database:
            self.assertEqual(database.execute('SELECT "SourceID" FROM extraction_pending').fetchall(), [(accession,)])
        with (directory / "metadata.csv").open(newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

    def test_export_excludes_stale_canonical_row_with_pending_review(self):
        with closing(sqlite3.connect(self.root / "canonical.sqlite")) as database:
            database.executescript("""
                CREATE TABLE canonical (Source TEXT, SourceID TEXT, Tissue TEXT, content_hash TEXT);
                INSERT INTO canonical VALUES ('GEO', 'GSM1234567', 'brain', 'hash1'),
                                             ('GEO', 'GSM1234568', 'lung', 'hash2');
                CREATE TABLE extraction_pending (Source TEXT, SourceID TEXT);
                INSERT INTO extraction_pending VALUES ('GEO', 'GSM1234568');
            """)
        result = self.run_cli("export", "--output", self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.root / "metadata.csv").open(newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [
                {"Source": "GEO", "SourceID": "GSM1234567", "Tissue": "brain"},
            ])


if __name__ == "__main__":
    unittest.main()
