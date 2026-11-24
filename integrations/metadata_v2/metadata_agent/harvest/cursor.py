from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class CursorStore:
    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cursors (
                db TEXT PRIMARY KEY,
                watermark TEXT NOT NULL,
                last_accession TEXT,
                run_ts TEXT
            )
        """)
        self._conn.commit()

    def get_window(self, db: str, lookback_days: int = 3) -> tuple[datetime, datetime]:
        row = self._conn.execute(
            "SELECT watermark FROM cursors WHERE db = ?", (db,)
        ).fetchone()

        now = datetime.now(timezone.utc)
        if row is None:
            start = now - timedelta(days=365)
        else:
            wm = datetime.fromisoformat(row[0])
            start = wm - timedelta(days=lookback_days)
        return start, now

    def get_last_accession(self, db: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_accession FROM cursors WHERE db = ?", (db,)
        ).fetchone()
        return row[0] if row else None

    def advance(self, db: str, watermark: datetime, last_accession: str | None = None) -> None:
        existing = self._conn.execute(
            "SELECT watermark FROM cursors WHERE db = ?", (db,)
        ).fetchone()

        if existing:
            existing_wm = datetime.fromisoformat(existing[0])
            if watermark < existing_wm:
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO cursors (db, watermark, last_accession, run_ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(db) DO UPDATE SET
                watermark = excluded.watermark,
                last_accession = excluded.last_accession,
                run_ts = excluded.run_ts
        """, (db, watermark.isoformat(), last_accession, now_iso))
        self._conn.commit()

    def list_all(self) -> list[dict[str, str | None]]:
        rows = self._conn.execute("SELECT db, watermark, last_accession, run_ts FROM cursors").fetchall()
        return [
            {"db": r[0], "watermark": r[1], "last_accession": r[2], "run_ts": r[3]}
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


class BackfillCursorStore:
    """Tracks full-history backfill progress in a dedicated table, separate from
    the incremental ``cursors`` watermark row (which must keep its monotonic
    365-day self-discovery behaviour untouched).

    A partition is a half-open date range ``[start, end)`` (or, for GSA which has
    no date filter, a single logical snapshot). It is marked ``complete`` only
    after every row it covers has been fetched and upserted, so an interrupted
    backfill re-runs only the partitions still ``pending``.
    """

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS backfill_partitions (
                mode TEXT NOT NULL,
                connector TEXT NOT NULL,
                query_fingerprint TEXT NOT NULL,
                partition_start TEXT NOT NULL,
                partition_end TEXT NOT NULL,
                status TEXT NOT NULL,
                last_sort_key TEXT,
                run_ts TEXT,
                PRIMARY KEY (mode, connector, query_fingerprint, partition_start, partition_end)
            )
        """)
        self._conn.commit()

    def register(
        self,
        mode: str,
        connector: str,
        query_fingerprint: str,
        partition_start: str,
        partition_end: str,
    ) -> None:
        """Insert a partition as ``pending`` if it is not already tracked."""
        self._conn.execute(
            """INSERT OR IGNORE INTO backfill_partitions
               (mode, connector, query_fingerprint, partition_start, partition_end, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (mode, connector, query_fingerprint, partition_start, partition_end),
        )
        self._conn.commit()

    def is_complete(
        self,
        mode: str,
        connector: str,
        query_fingerprint: str,
        partition_start: str,
        partition_end: str,
    ) -> bool:
        row = self._conn.execute(
            """SELECT status FROM backfill_partitions
               WHERE mode = ? AND connector = ? AND query_fingerprint = ?
                 AND partition_start = ? AND partition_end = ?""",
            (mode, connector, query_fingerprint, partition_start, partition_end),
        ).fetchone()
        return row is not None and row[0] == "complete"

    def update_progress(
        self,
        mode: str,
        connector: str,
        query_fingerprint: str,
        partition_start: str,
        partition_end: str,
        last_sort_key: str,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE backfill_partitions
               SET last_sort_key = ?, run_ts = ?
               WHERE mode = ? AND connector = ? AND query_fingerprint = ?
                 AND partition_start = ? AND partition_end = ?""",
            (last_sort_key, now_iso, mode, connector, query_fingerprint,
             partition_start, partition_end),
        )
        self._conn.commit()

    def mark_complete(
        self,
        mode: str,
        connector: str,
        query_fingerprint: str,
        partition_start: str,
        partition_end: str,
    ) -> None:
        """Transactionally flag a partition ``complete``. Callers MUST invoke
        this only after all of the partition's rows are durably upserted."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE backfill_partitions
               SET status = 'complete', run_ts = ?
               WHERE mode = ? AND connector = ? AND query_fingerprint = ?
                 AND partition_start = ? AND partition_end = ?""",
            (now_iso, mode, connector, query_fingerprint,
             partition_start, partition_end),
        )
        self._conn.commit()

    def list_partitions(
        self, mode: str, connector: str, query_fingerprint: str
    ) -> list[dict[str, str | None]]:
        rows = self._conn.execute(
            """SELECT partition_start, partition_end, status, last_sort_key, run_ts
               FROM backfill_partitions
               WHERE mode = ? AND connector = ? AND query_fingerprint = ?
               ORDER BY partition_start""",
            (mode, connector, query_fingerprint),
        ).fetchall()
        return [
            {"partition_start": r[0], "partition_end": r[1], "status": r[2],
             "last_sort_key": r[3], "run_ts": r[4]}
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


def date_partitions(
    since: datetime, until: datetime, partition_days: int = 30
) -> list[tuple[datetime, datetime]]:
    """Split ``[since, until)`` into deterministic oldest-first half-open date
    partitions of ``partition_days`` each. The final partition is clamped to
    ``until`` so the union covers the request range exactly with no overlap.
    """
    if partition_days < 1:
        raise ValueError("partition_days must be >= 1")
    if until <= since:
        return []
    parts: list[tuple[datetime, datetime]] = []
    cursor = since
    step = timedelta(days=partition_days)
    while cursor < until:
        nxt = min(cursor + step, until)
        parts.append((cursor, nxt))
        cursor = nxt
    return parts
