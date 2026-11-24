from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from metadata_agent.store.db import SingleWriterDB

_RAW_MIGRATION = """
CREATE TABLE IF NOT EXISTS raw_metadata (
    accession TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_fetched_at ON raw_metadata(fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_content_hash ON raw_metadata(content_hash);
"""


def _compute_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RawStore:
    def __init__(self, db_name: str, data_dir: str | Path = "data"):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / f"raw_{db_name}.sqlite"
        self._db = SingleWriterDB(db_path, migrations=[_RAW_MIGRATION])
        self._db_name = db_name

    def upsert(self, record: dict[str, Any]) -> str:
        accession = record["accession"]
        source = record["source"]
        source_url = record["source_url"]
        fetched_at = record["fetched_at"]
        payload = json.dumps(record["raw"], ensure_ascii=False, sort_keys=True)
        content_hash = _compute_hash(payload)

        existing = self._db.read_one(
            "SELECT content_hash FROM raw_metadata WHERE accession = ?",
            (accession,),
        )

        if existing is None:
            self._db.write(
                """INSERT INTO raw_metadata (accession, source, source_url, fetched_at, content_hash, payload)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (accession, source, source_url, fetched_at, content_hash, payload),
            )
            return "inserted"

        if existing["content_hash"] == content_hash:
            self._db.write(
                "UPDATE raw_metadata SET fetched_at = ? WHERE accession = ?",
                (fetched_at, accession),
            )
            return "unchanged"

        self._db.write(
            """UPDATE raw_metadata SET source_url = ?, fetched_at = ?, content_hash = ?, payload = ?
               WHERE accession = ?""",
            (source_url, fetched_at, content_hash, payload, accession),
        )
        return "updated"

    def get(self, accession: str) -> dict[str, Any] | None:
        row = self._db.read_one(
            "SELECT * FROM raw_metadata WHERE accession = ?", (accession,)
        )
        if row is None:
            return None
        return {
            "accession": row["accession"],
            "source": row["source"],
            "source_url": row["source_url"],
            "fetched_at": row["fetched_at"],
            "content_hash": row["content_hash"],
            "raw": json.loads(row["payload"]),
        }

    def get_content_hash(self, accession: str) -> str | None:
        row = self._db.read_one(
            "SELECT content_hash FROM raw_metadata WHERE accession = ?", (accession,)
        )
        return row["content_hash"] if row else None

    def iter_all(self) -> list[dict[str, Any]]:
        rows = self._db.read("SELECT * FROM raw_metadata")
        return [
            {
                "accession": row["accession"],
                "source": row["source"],
                "source_url": row["source_url"],
                "fetched_at": row["fetched_at"],
                "content_hash": row["content_hash"],
                "raw": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def count(self) -> int:
        row = self._db.read_one("SELECT COUNT(*) as cnt FROM raw_metadata")
        return row["cnt"] if row else 0

    def close(self) -> None:
        self._db.close()
