"""Thread-safe persistent cache of successful public-source responses."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from metadata_agent.runtime import KeyedLocks, count


class SourceCache:
    def __init__(self, path: Path, ttl_seconds: float = 86400, *, refresh: bool = False):
        if not math.isfinite(ttl_seconds) or ttl_seconds < 0:
            raise ValueError("Source cache TTL must be a finite nonnegative number")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds
        self._fresh_since = time.time() if refresh else 0.0
        self._lock = threading.Lock()
        self._requests = KeyedLocks()
        # Only short database operations hold _lock; fetches use per-key locks.
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS source_cache (
            namespace TEXT NOT NULL, cache_key TEXT NOT NULL,
            payload TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (namespace, cache_key)
        )""")
        self._conn.commit()

    def get_or_load(self, namespace: str, key: Any, loader: Callable[[], Any],
                    valid: Callable[[Any], bool] = bool) -> Any:
        encoded = json.dumps(key, sort_keys=True, ensure_ascii=False)
        with self._requests.hold((namespace, encoded)):
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload, fetched_at FROM source_cache WHERE namespace = ? AND cache_key = ?",
                    (namespace, encoded),
                ).fetchone()
            if row and self._ttl > 0 and row[1] >= self._fresh_since and time.time() - row[1] < self._ttl:
                try:
                    value = json.loads(row[0])
                    if valid(value):
                        count("source_cache_hits")
                        return value
                except (ValueError, TypeError):
                    pass
            count("source_cache_misses")
            value = loader()
            if valid(value):
                payload = json.dumps(value, ensure_ascii=False)
                with self._lock:
                    with self._conn:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO source_cache VALUES (?, ?, ?, ?)",
                            (namespace, encoded, payload, time.time()),
                        )
            return value

    def close(self) -> None:
        with self._lock:
            self._conn.close()
