from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunLog:
    def __init__(self, data_dir: str | Path):
        self._path = Path(data_dir) / "run_log.sqlite"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_ts TEXT NOT NULL,
                db TEXT NOT NULL,
                fetched INTEGER DEFAULT 0,
                upserted INTEGER DEFAULT 0,
                skipped_dup INTEGER DEFAULT 0,
                llm_calls INTEGER DEFAULT 0,
                retries_429 INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                duration_s REAL DEFAULT 0.0,
                error TEXT
            )
        """)
        self._conn.commit()

    def log_run(
        self,
        db: str,
        stats: dict[str, Any],
        duration_s: float = 0.0,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO run_log
               (run_ts, db, fetched, upserted, skipped_dup,
                llm_calls, retries_429, cost_usd, duration_s, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                db,
                stats.get("fetched", 0),
                stats.get("upserted", 0),
                stats.get("skipped", 0),
                stats.get("llm_calls", 0),
                stats.get("retries_429", 0),
                stats.get("cost_usd", 0.0),
                duration_s,
                error,
            ),
        )
        self._conn.commit()

    def summary_line(self, db: str, stats: dict[str, Any]) -> str:
        return (
            f"[{db}] fetched={stats.get('fetched', 0)} "
            f"upserted={stats.get('upserted', 0)} "
            f"skipped={stats.get('skipped', 0)} "
            f"llm={stats.get('llm_calls', 0)} "
            f"429s={stats.get('retries_429', 0)}"
        )

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
