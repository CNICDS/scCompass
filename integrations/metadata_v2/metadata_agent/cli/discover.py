"""CLI ``discover`` command — organism-driven cross-DB discovery + enrichment.

Given an organism, discovers all its samples across the discovery databases
(GEO/SRA/ENA/ArrayExpress/GSA — BioSample is a hub, not a discovery source),
fans study-level results down to sample/run roots, enriches each root via the
cross-link resolver, extracts, and upserts. ``Source``/``SourceID`` are forced
from the discovery root after enrichment so identity is never merged.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any

from metadata_agent.config.loader import load_config
from metadata_agent.connectors.base import get_connector
from metadata_agent.connectors.http_client import HttpClient
from metadata_agent.connectors.key_pool import NCBIKeyPool
from metadata_agent.connectors.rate_limiter import RateLimiterRegistry
from metadata_agent.enrich.resolver import CrossLinkResolver, enrich_record
from metadata_agent.extract.fanout import fanout_samples
from metadata_agent.store.source_mapping import connector_to_source

logger = logging.getLogger("metadata_agent")

# BioSample is an enrichment hub, not a discovery source (design D1/§11.3).
DISCOVERY_DBS = ("geo", "sra", "ena", "arrayexpress", "gsa")


def _discovery_config(db_conf: dict[str, Any], organism: str) -> dict[str, Any]:
    """A per-connector config carrying the organism, built without mutating the
    caller's config object (spec: discovery must not mutate global config)."""
    conf = copy.deepcopy(db_conf)
    conf["organism"] = organism
    return conf


def discover_organism(
    organism: str,
    config: dict[str, Any],
    profile_name: str,
    db_filter: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    http: HttpClient | None = None,
) -> dict[str, dict[str, Any]]:
    """Discover + enrich samples for ``organism`` across the discovery DBs.

    Returns ``{f"{connector}:{accession}": canonical_record}``. In ``dry_run``
    only discovery runs (no fetch/enrich/extract), and the value is a stub
    carrying the discovered root accession.
    """
    from metadata_agent.cli.fetch import _build_pipeline

    databases = config.get("databases", {})
    dbs = [d for d in DISCOVERY_DBS if (not db_filter or d in db_filter)]
    dbs = [d for d in dbs if databases.get(d, {}).get("enabled", True)]

    http = http or HttpClient(rate_registry=RateLimiterRegistry(), key_pool=NCBIKeyPool())
    pipeline = None if dry_run else _build_pipeline(config, profile_name)
    resolver = None if dry_run else CrossLinkResolver(
        http, pipeline,
        databases_config=databases,
        max_siblings=int(config.get("enrichment", {}).get("max_siblings", 8)),
    )

    out: dict[str, dict[str, Any]] = {}
    seen_roots: set[tuple[str, str]] = set()

    for db_name in dbs:
        db_conf = _discovery_config(databases.get(db_name, {}), organism)
        connector = get_connector(db_name)(config=db_conf, http_client=http)

        try:
            roots = sorted(set(connector.search()))
        except Exception as exc:
            logger.error("discover %s search failed: %s", db_name, exc)
            continue

        if limit is not None and len(roots) > limit:
            omitted = len(roots) - limit
            logger.warning(
                "discover %s: --limit truncated %d discovered roots (kept %d, omitted %d)",
                db_name, len(roots), limit, omitted,
            )
            roots = roots[:limit]

        source = connector_to_source(db_name)

        for root_acc in roots:
            if dry_run:
                key = f"{db_name}:{root_acc}"
                out[key] = {"Source": source, "SourceID": root_acc, "_dry_run": True}
                continue

            try:
                raw = connector.fetch(root_acc)
            except Exception as exc:
                logger.error("discover %s fetch %s failed: %s", db_name, root_acc, exc)
                continue
            if raw is None or "error" in raw.get("raw", {}):
                continue

            for sample_acc, sample_raw in fanout_samples(raw, db_name):
                if not sample_acc:
                    continue
                dedup_key = (db_name, sample_acc)
                if dedup_key in seen_roots:
                    continue
                seen_roots.add(dedup_key)

                try:
                    record = enrich_record(sample_raw, db_name, pipeline, resolver)
                except Exception as exc:
                    logger.error("discover extract %s/%s failed: %s", source, sample_acc, exc)
                    continue
                if record is None:
                    continue

                # Identity is forced from the discovery root after enrichment;
                # siblings never rewrite Source/SourceID (identity is not merged).
                record["Source"] = source
                record["SourceID"] = sample_acc
                for meta in ("_audit_log", "_diagnostics", "_status",
                             "content_hash", "profile_fingerprint"):
                    record.pop(meta, None)
                out[f"{db_name}:{sample_acc}"] = record

    return out


def add_discover_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "discover",
        help="Discover all samples for an organism across databases and enrich each",
    )
    p.add_argument("--organism", required=True, help="Organism name (e.g. 'Homo sapiens')")
    p.add_argument("--db", help="Comma-separated discovery DBs (default: all)")
    p.add_argument("--backfill", action="store_true", help="Full-history discovery (requires --since)")
    p.add_argument("--since", help="Backfill start date (ISO); required with --backfill")
    p.add_argument("--limit", type=int, help="Max discovered roots per connector")
    p.add_argument("--dry-run", action="store_true", help="Only discover roots; do not fetch/enrich")
    p.add_argument("--output", "-o", help="Write JSON output to FILE instead of stdout")
    p.add_argument("--profile", help="Profile name (default: active_profile)")
    p.set_defaults(func=cmd_discover)


def cmd_discover(args: argparse.Namespace) -> None:
    if args.backfill and not args.since:
        print("error: --backfill requires --since", file=sys.stderr)
        sys.exit(2)

    config = load_config(Path(args.config))
    profile_name = args.profile or config.get("active_profile", "human_cellline")
    db_filter = [d.strip() for d in args.db.split(",")] if args.db else None

    records = discover_organism(
        organism=args.organism,
        config=config,
        profile_name=profile_name,
        db_filter=db_filter,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    rendered = json.dumps(list(records.values()), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(rendered)
    else:
        print(rendered)

    print(f"Discovered {len(records)} sample(s) for {args.organism!r}", file=sys.stderr)
    sys.exit(0 if records else 1)
