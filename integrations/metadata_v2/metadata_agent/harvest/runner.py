from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from metadata_agent.connectors.base import SourceConnector, get_connector
from metadata_agent.connectors.http_client import HttpClient
from metadata_agent.connectors.query_builder import (
    QueryConfigError,
    organism_filters_query,
    resolve_organisms,
    resolve_query,
)
from metadata_agent.harvest.cursor import (
    BackfillCursorStore,
    CursorStore,
    date_partitions,
)
from metadata_agent.store.raw import RawStore

logger = logging.getLogger(__name__)


def _configured_organisms(config: dict[str, Any]) -> list[str]:
    """Organisms that actually change this config's query — none for a legacy
    ``search_queries`` literal, which the organism does not filter."""
    return resolve_organisms(config) if organism_filters_query(config) else []


def _query_fingerprint(connector: str, config: dict[str, Any]) -> str:
    """Stable fingerprint of the query a connector will run, so backfill
    partition state is invalidated when the discovery query — or the organism
    list it fans out over — changes. The organism-free query is hashed with the
    *sorted* organism list, so widening the list invalidates cursors while
    merely reordering it does not."""
    try:
        organisms = _configured_organisms(config)
        query = resolve_query(connector, dict(config, organism=[]))
    except QueryConfigError as exc:
        # A query the builder refuses still needs a distinct partition
        # namespace, so the rejected organism value stays in the digest.
        logger.warning("Harvest %s: discovery config invalid (%s)", connector, exc)
        organisms = [repr(config.get("organism"))]
        query = str(config.get("search_queries") or config.get("search_query") or "")
    material = json.dumps(
        {"query": query, "organisms": sorted(organisms)}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class HarvestRunner:
    def __init__(
        self,
        cursor_store: CursorStore,
        http_client: HttpClient,
        data_dir: str,
        lookback_days: int = 3,
        limit: int | None = None,
    ):
        self._cursors = cursor_store
        self._http = http_client
        self._data_dir = data_dir
        self._lookback_days = lookback_days
        self._limit = limit

    def _search_organisms(
        self,
        db_name: str,
        connector_cls: type,
        config: dict[str, Any],
        since: str,
        until: str,
    ) -> tuple[list[str], int]:
        """Search once per configured organism and union the accessions.

        Organisms are queried serially so each one's count is observable and one
        organism's failure is only counted, never fatal to the rest. The union is
        de-duplicated before fetching, so an accession shared by two species is
        fetched once.
        """
        found: set[str] = set()
        failed = 0

        for organism in _configured_organisms(config) or [None]:
            conf = config if organism is None else {**config, "organism": organism}
            try:
                connector: SourceConnector = connector_cls(config=conf, http_client=self._http)
                accessions = set(connector.search(since, until))
            except Exception as exc:
                failed += 1
                logger.error(
                    "Harvest %s organism=%s: discovered=0 failed=1 (%s)",
                    db_name, organism, exc,
                )
                continue
            logger.info(
                "Harvest %s organism=%s: discovered=%d", db_name, organism, len(accessions)
            )
            found |= accessions

        return sorted(found), failed

    def run_connector(self, db_name: str, config: dict[str, Any]) -> dict[str, int]:
        try:
            connector_cls = get_connector(db_name)
        except KeyError:
            logger.error("No connector registered for %s", db_name)
            return {"fetched": 0, "upserted": 0, "skipped": 0, "failed": 0, "search_failed": 0}

        connector: SourceConnector = connector_cls(config=config, http_client=self._http)
        raw_store = RawStore(db_name=db_name, data_dir=self._data_dir)

        start, end = self._cursors.get_window(db_name, self._lookback_days)
        last_acc = self._cursors.get_last_accession(db_name)

        # ``failed`` counts unfetchable accessions; a dead organism costs a whole
        # slice of the window, so it is counted apart from it.
        stats = {"fetched": 0, "upserted": 0, "skipped": 0, "failed": 0, "search_failed": 0}
        window_complete = False
        latest_accession: str | None = None

        try:
            since_str = start.strftime("%Y/%m/%d")
            until_str = end.strftime("%Y/%m/%d")

            # A multi-term OR / date-partitioned query does not guarantee a
            # stable result order across runs, so processing is driven by a
            # deterministic sort — never the connector's raw yield order. An
            # aborted window is not committed to the watermark, so the next run
            # re-scans the whole window; idempotent raw upsert makes the
            # already-fetched rows no-ops (resume without a fragile order-index
            # slice, and without risking a cross-window recall skip).
            accessions, search_failures = self._search_organisms(
                db_name, connector_cls, config, since_str, until_str
            )
            stats["search_failed"] = search_failures

            # --limit truncates the whole species union, never one organism's
            # share of it.
            if self._limit:
                accessions = accessions[: self._limit]

            for acc in accessions:
                stats["fetched"] += 1
                raw = connector.fetch(acc)
                if raw is None:
                    stats["failed"] += 1
                    continue

                normalized = connector.normalize(raw)
                status = raw_store.upsert(normalized)
                if status in ("inserted", "updated"):
                    stats["upserted"] += 1
                else:
                    stats["skipped"] += 1

                latest_accession = acc

            # An organism whose search failed leaves part of the window
            # undiscovered, so the watermark must not move past it.
            window_complete = search_failures == 0

        except Exception as exc:
            logger.error("Harvest %s aborted: %s", db_name, exc)

        # Advance the watermark only when the whole window drained without an
        # abort. A partial run leaves the watermark untouched so the next run
        # re-scans the window; idempotent upsert prevents duplicates. Within a
        # completed window we still record the last accession so a subsequent
        # equal-window run can resume past it.
        if window_complete:
            self._cursors.advance(db_name, end, latest_accession or last_acc)

        raw_store.close()
        logger.info(
            "Harvest %s: fetched=%d upserted=%d skipped=%d failed=%d search_failed=%d",
            db_name, stats["fetched"], stats["upserted"], stats["skipped"],
            stats["failed"], stats["search_failed"],
        )
        return stats

    def run_backfill(
        self,
        db_name: str,
        config: dict[str, Any],
        backfill_store: BackfillCursorStore,
        since: str,
        until: str | None = None,
        partition_days: int = 30,
    ) -> dict[str, int]:
        """Harvest the full ``[since, until)`` history in oldest-first date
        partitions, resumably. A partition is marked ``complete`` only after all
        of its rows are durably upserted, so an interrupted run re-processes only
        the partitions still ``pending`` — the incremental ``cursors`` watermark
        is never touched.

        ``since``/``until`` are ISO date strings; ``until`` defaults to now.
        GSA has no server-side date filter, so its whole history is one logical
        partition (a resumable paginated browse snapshot) spanning the range.
        """
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        if until:
            until_dt = datetime.fromisoformat(until)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
        else:
            until_dt = datetime.now(timezone.utc)

        try:
            connector_cls = get_connector(db_name)
        except KeyError:
            logger.error("No connector registered for %s", db_name)
            return {"fetched": 0, "upserted": 0, "skipped": 0, "failed": 0, "partitions": 0}

        connector: SourceConnector = connector_cls(config=config, http_client=self._http)
        raw_store = RawStore(db_name=db_name, data_dir=self._data_dir)
        fp = _query_fingerprint(db_name, config)

        if db_name == "gsa":
            partitions = [(since_dt, until_dt)]
        else:
            partitions = date_partitions(since_dt, until_dt, partition_days)

        stats = {"fetched": 0, "upserted": 0, "skipped": 0, "failed": 0, "partitions": 0}

        for p_start, p_end in partitions:
            start_key = p_start.strftime("%Y/%m/%d")
            end_key = p_end.strftime("%Y/%m/%d")

            backfill_store.register("backfill", db_name, fp, start_key, end_key)
            if backfill_store.is_complete("backfill", db_name, fp, start_key, end_key):
                logger.info("backfill %s: skip complete partition [%s, %s)", db_name, start_key, end_key)
                continue

            partition_ok = False
            latest_key: str | None = None
            try:
                if db_name == "gsa":
                    accessions = sorted(set(connector.search()))
                else:
                    accessions = sorted(set(connector.search(start_key, end_key)))

                for acc in accessions:
                    stats["fetched"] += 1
                    raw = connector.fetch(acc)
                    if raw is None:
                        stats["failed"] += 1
                        continue
                    status = raw_store.upsert(connector.normalize(raw))
                    if status in ("inserted", "updated"):
                        stats["upserted"] += 1
                    else:
                        stats["skipped"] += 1
                    latest_key = acc
                    backfill_store.update_progress(
                        "backfill", db_name, fp, start_key, end_key, acc
                    )
                partition_ok = True
            except Exception as exc:
                logger.error(
                    "backfill %s partition [%s, %s) aborted: %s",
                    db_name, start_key, end_key, exc,
                )

            # A partition is complete only when its whole accession set drained
            # without an abort; a partial partition stays pending and is re-run.
            if partition_ok:
                if latest_key is not None:
                    backfill_store.update_progress(
                        "backfill", db_name, fp, start_key, end_key, latest_key
                    )
                backfill_store.mark_complete("backfill", db_name, fp, start_key, end_key)
                stats["partitions"] += 1

        raw_store.close()
        logger.info(
            "Backfill %s: partitions=%d fetched=%d upserted=%d skipped=%d failed=%d",
            db_name, stats["partitions"], stats["fetched"],
            stats["upserted"], stats["skipped"], stats["failed"],
        )
        return stats
