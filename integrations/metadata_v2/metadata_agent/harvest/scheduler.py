from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from metadata_agent.config.loader import load_config
from metadata_agent.connectors.http_client import HttpClient
from metadata_agent.connectors.key_pool import NCBIKeyPool
from metadata_agent.connectors.rate_limiter import RateLimiterRegistry
from metadata_agent.harvest.cursor import BackfillCursorStore, CursorStore
from metadata_agent.harvest.lock import HarvestLock
from metadata_agent.harvest.runner import HarvestRunner

logger = logging.getLogger(__name__)


def harvest_all(
    config_path: str = "config.yaml",
    db_filter: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    backfill: bool = False,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, dict[str, int]]:
    config = load_config(config_path)
    data_dir = config.get("storage", {}).get("data_dir", "data")
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    # Backfill harvests full history and must be told where to start; refuse an
    # implicit all-history crawl (design D4).
    if backfill and not since:
        raise ValueError("--backfill requires an explicit --since date")

    lock_path = config.get("harvest", {}).get("lock_file", f"{data_dir}/.harvest.lock")
    lock = HarvestLock(lock_path)

    if not lock.acquire():
        return {}

    try:
        rate_registry = RateLimiterRegistry()
        key_pool = NCBIKeyPool()
        http_client = HttpClient(rate_registry=rate_registry, key_pool=key_pool)
        cursor_store = CursorStore(Path(data_dir) / "cursors.sqlite")

        lookback = config.get("harvest", {}).get("lookback_days", 3)
        partition_days = config.get("harvest", {}).get("partition_days", 30)
        runner = HarvestRunner(
            cursor_store=cursor_store,
            http_client=http_client,
            data_dir=data_dir,
            lookback_days=lookback,
            limit=limit,
        )

        backfill_store: BackfillCursorStore | None = None
        if backfill:
            backfill_store = BackfillCursorStore(Path(data_dir) / "cursors.sqlite")

        results: dict[str, dict[str, int]] = {}
        databases: dict[str, Any] = config.get("databases", {})

        for db_name, db_conf in databases.items():
            if not db_conf.get("enabled", True):
                continue
            if db_filter and db_name != db_filter:
                continue

            logger.info("Harvesting %s...", db_name)
            if dry_run:
                logger.info("[DRY RUN] Would harvest %s", db_name)
                results[db_name] = {"fetched": 0, "upserted": 0, "skipped": 0, "failed": 0}
            elif backfill:
                results[db_name] = runner.run_backfill(
                    db_name, db_conf, backfill_store,
                    since=since, until=until, partition_days=partition_days,
                )
            else:
                results[db_name] = runner.run_connector(db_name, db_conf)

        if backfill_store is not None:
            backfill_store.close()
        cursor_store.close()
        return results
    finally:
        lock.release()
