from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_WRITE_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()

# SQLite stores INTEGER as a signed 64-bit value; binding anything wider raises
# OverflowError. Callers that produce integers check against this before writing.
SQLITE_INT_MIN = -9223372036854775808
SQLITE_INT_MAX = 9223372036854775807


def _get_write_lock(db_path: str) -> threading.Lock:
    with _LOCK_GUARD:
        if db_path not in _WRITE_LOCKS:
            _WRITE_LOCKS[db_path] = threading.Lock()
        return _WRITE_LOCKS[db_path]


def open_db(db_path: str | Path, migrations: list[str] | None = None) -> sqlite3.Connection:
    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    if migrations:
        for sql in migrations:
            conn.executescript(sql)
    return conn


class SingleWriterDB:
    def __init__(self, db_path: str | Path, migrations: list[str] | None = None):
        self._db_path = str(db_path)
        self._lock = _get_write_lock(self._db_path)
        self._conn = open_db(self._db_path, migrations)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def write_many(self, sql: str, params_list: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, params_list)
            self._conn.commit()

    def read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def read_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self._conn.close()
