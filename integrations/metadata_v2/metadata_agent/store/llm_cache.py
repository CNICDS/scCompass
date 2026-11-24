from __future__ import annotations

import json
from pathlib import Path

from metadata_agent.store.db import open_db


class LLMCache:
    def __init__(self, db_path: str | Path = "data/canonical.sqlite"):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = open_db(str(self._path))
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                content_hash TEXT NOT NULL,
                prompt_fingerprint TEXT NOT NULL,
                model_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (content_hash, prompt_fingerprint, model_id)
            )
        """)
        self._migrate_drop_profile_fingerprint()
        self._conn.commit()

    def _migrate_drop_profile_fingerprint(self) -> None:
        """Migrate legacy cache keyed on profile_fingerprint.

        The old primary key included profile_fingerprint; decoupling the LLM
        cache from the profile means that column no longer participates in the
        key. Preserve existing cached LLM outputs by collapsing them onto the
        new (content_hash, prompt_fingerprint, model_id) key.
        """
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(llm_cache)").fetchall()]
        if "profile_fingerprint" not in cols:
            return
        # Rebuild the table under the new key atomically: a crash mid-migration
        # must leave either the old table or the fully migrated one, never a
        # half-copied state.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DROP TABLE IF EXISTS llm_cache_new")
            self._conn.execute("""
                CREATE TABLE llm_cache_new (
                    content_hash TEXT NOT NULL,
                    prompt_fingerprint TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (content_hash, prompt_fingerprint, model_id)
                )
            """)
            self._conn.execute("""
                INSERT OR REPLACE INTO llm_cache_new
                    (content_hash, prompt_fingerprint, model_id, result_json, created_at)
                SELECT content_hash, prompt_fingerprint, model_id, result_json, created_at
                FROM llm_cache
            """)
            self._conn.execute("DROP TABLE llm_cache")
            self._conn.execute("ALTER TABLE llm_cache_new RENAME TO llm_cache")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get(
        self,
        content_hash: str,
        prompt_fingerprint: str,
        model_id: str,
    ) -> dict | None:
        row = self._conn.execute(
            """SELECT result_json FROM llm_cache
               WHERE content_hash = ? AND prompt_fingerprint = ? AND model_id = ?""",
            (content_hash, prompt_fingerprint, model_id),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def put(
        self,
        content_hash: str,
        prompt_fingerprint: str,
        model_id: str,
        result: dict,
    ) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO llm_cache
               (content_hash, prompt_fingerprint, model_id, result_json)
               VALUES (?, ?, ?, ?)""",
            (content_hash, prompt_fingerprint, model_id, json.dumps(result, ensure_ascii=False)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
