from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from metadata_agent.config.loader import load_config
from metadata_agent.config.secrets import RedactingFilter

logger = logging.getLogger("metadata_agent")


def _setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    fmt = log_cfg.get("format", "json")

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())

    if fmt == "json":
        handler.setFormatter(logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
        ))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


def _resolve_profile_dir(config: dict, profile_name: str | None = None) -> Path:
    return Path("profiles") / (profile_name or config.get("active_profile", "human_cellline"))


def _add_rebuild_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-canonical-rebuild",
        action="store_true",
        help="Allow a destructive canonical-table rebuild when the profile schema changes",
    )


def _ontology_enabled(config: dict) -> bool:
    return bool(config.get("ontology", {}).get("enabled", False))


def _build_ontology_matcher(config: dict, schema: Any, canonical_path: Path, http_client: Any):
    """Construct the matcher when alignment is configured on, else ``None``.

    ``ontology_cache`` is created in the canonical database with the other
    sidecars, so the cache is always read from there; pointing it at a second
    file would make every lookup fail on a missing table.
    """
    from metadata_agent.ontology.matcher import OntologyMatcher

    if not _ontology_enabled(config):
        return None
    return OntologyMatcher(
        schema=schema,
        ontology_versions=config.get("ontology", {}).get("versions", {}),
        cache_db=canonical_path,
        http_client=http_client,
    )


def cmd_harvest(args: argparse.Namespace) -> None:
    from metadata_agent.harvest.scheduler import harvest_all

    if args.backfill and not args.since:
        print(
            "error: --backfill requires an explicit --since (no implicit "
            "all-history crawl)",
            file=sys.stderr,
        )
        sys.exit(2)

    results = harvest_all(
        config_path=args.config,
        db_filter=args.db,
        limit=args.limit,
        dry_run=args.dry_run,
        backfill=args.backfill,
        since=args.since,
        until=args.until,
    )
    if results:
        for db, stats in results.items():
            logger.info("harvest %s: %s", db, json.dumps(stats))


def cmd_extract(args: argparse.Namespace) -> None:
    if args.max_llm_calls < 0:
        raise ValueError("--max-llm-calls must be nonnegative (per sample)")
    from metadata_agent.config.fingerprint import (
        compute_profile_fingerprint,
        compute_prompt_fingerprint,
        with_llm_transport,
    )
    from metadata_agent.config.llm import resolve_llm_settings
    from metadata_agent.config.loader import load_config, load_profile, resolve_trigger_fields
    from metadata_agent.config.tiers import tiered_fields_from_config
    from metadata_agent.connectors.http_client import HttpClient
    from metadata_agent.connectors.key_pool import NCBIKeyPool
    from metadata_agent.connectors.rate_limiter import RateLimiterRegistry
    from metadata_agent.enrich import CrossLinkResolver, enrich_record
    from metadata_agent.extract.llm import LLMExtractor
    from metadata_agent.extract.pipeline import ExtractionPipeline
    from metadata_agent.extract.rules import RulesExtractor, load_rules
    from metadata_agent.extract.tiers import TierExtractor
    from metadata_agent.store.canonical import CanonicalStore
    from metadata_agent.store.llm_cache import LLMCache
    from metadata_agent.store.raw import RawStore
    from metadata_agent.store.source_mapping import connector_to_source as map_source

    config = load_config(Path(args.config))
    profile_dir = _resolve_profile_dir(config, args.profile)
    tiered_fields = tiered_fields_from_config(config)
    profile = load_profile(profile_dir, tiered_fields=tiered_fields)

    schema = profile["schema"]
    from metadata_agent.config.loader import load_vocab_sets
    enums, aliases = load_vocab_sets(profile_dir, schema)

    rules = load_rules(str(profile_dir / "rules.yaml"))
    rules_extractor = RulesExtractor(rules, schema=schema)

    settings = resolve_llm_settings(config, api_mode=getattr(args, "llm_api", None)) if args.with_llm else None
    model_id = settings.model if settings else ""
    profile_fp = compute_profile_fingerprint(profile_dir)
    prompt_fp = compute_prompt_fingerprint(profile_dir, model_id)

    llm_extractor = None
    if args.with_llm:
        logger.info("LLM call limit per sample: %d (including its enrichment)", args.max_llm_calls)
        profile_fp = with_llm_transport(profile_fp, settings.cache_identity)
        prompt_fp = with_llm_transport(prompt_fp, settings.cache_identity)
        logger.info("LLM transport: %s, API root: %s, model: %s", settings.api_mode, settings.base_url, model_id)
        llm_extractor = LLMExtractor(
            profile_dir=str(profile_dir),
            max_calls=args.max_llm_calls,
            model=model_id,
            api_mode=settings.api_mode,
            base_url=settings.base_url,
        )

    data_dir = config.get("storage", {}).get("data_dir", "data")
    canonical_path = Path(data_dir) / "canonical.sqlite"
    canonical = CanonicalStore(
        canonical_path,
        profile_dir=profile_dir,
        allow_destructive_rebuild=args.allow_canonical_rebuild,
    )
    llm_cache = LLMCache(Path(data_dir) / "canonical.sqlite")

    enum_column_names = {col.name for col in schema.columns if col.type in ("enum", "semi")}
    trigger_fields = resolve_trigger_fields(config, schema)

    # Shared HTTP client + cross-link resolver: enrichment sibling fetches and
    # ontology lookups share the aggregate per-host rate limit with everything
    # else in this process.
    http = HttpClient(rate_registry=RateLimiterRegistry(), key_pool=NCBIKeyPool())

    ontology_matcher = _build_ontology_matcher(config, schema, canonical_path, http)

    pipeline = ExtractionPipeline(
        rules_extractor=rules_extractor,
        llm_extractor=llm_extractor,
        vocab_sets=enums,
        vocab_aliases=aliases,
        max_llm_calls=args.max_llm_calls,
        profile_fingerprint=profile_fp,
        prompt_fingerprint=prompt_fp,
        model_id=model_id,
        llm_cache=llm_cache,
        enum_columns=enum_column_names,
        trigger_fields=trigger_fields,
        schema=schema,
        ontology_matcher=ontology_matcher,
        tier_extractor=TierExtractor(config, schema, http),
    )

    databases = config.get("databases", {})
    db_list = [args.db] if args.db else [k for k, v in databases.items() if v.get("enabled", True)]

    resolver = CrossLinkResolver(http, pipeline, databases_config=databases)

    total_stats = {"extracted": 0, "skipped": 0, "deferred": 0, "failed": 0, "llm_calls": 0}

    from metadata_agent.extract.fanout import fanout_samples

    for db_name in db_list:
        raw_store = RawStore(db_name=db_name, data_dir=data_dir)
        source = map_source(db_name)

        for row in raw_store.iter_all():
            samples = fanout_samples(row, db_name)

            for sample_accession, sample_record in samples:
                if not sample_accession:
                    continue
                sample_record.setdefault("accession", sample_accession)

                stored_hash = canonical.get_content_hash(source, sample_accession)
                stored_fp = canonical.get_profile_fingerprint(source, sample_accession)

                try:
                    result = enrich_record(
                        sample_record, db_name, pipeline, resolver,
                        stored_hash=stored_hash, stored_fingerprint=stored_fp,
                    )
                except Exception as exc:
                    logger.error("extract failed for %s/%s: %s", source, sample_accession, exc)
                    total_stats["failed"] += 1
                    continue

                if result is None:
                    total_stats["skipped"] += 1
                    continue

                result["Source"] = source
                result["SourceID"] = sample_accession
                try:
                    committed = canonical.upsert(result)
                except Exception as exc:
                    logger.error("upsert failed for %s/%s: %s", source, sample_accession, exc)
                    total_stats["failed"] += 1
                    continue
                # A deferred record wrote only its pending row: the ontology was
                # unreachable, so committing would have stamped a content hash
                # that skips the sample forever.
                total_stats["extracted" if committed else "deferred"] += 1

        raw_store.close()

    total_stats["llm_calls"] = pipeline.llm_calls_used
    pipeline.close()
    llm_cache.close()
    canonical.close()
    logger.info("extract: %s", json.dumps(total_stats))


def cmd_export(args: argparse.Namespace) -> None:
    from metadata_agent.store.export import export_xlsx

    config = load_config(args.config)
    data_dir = config.get("storage", {}).get("data_dir", "data")
    profile_dir = _resolve_profile_dir(config)
    output = args.output or config.get("output", {}).get("path", "output/metadata_export.xlsx")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    export_xlsx(
        db_path=str(Path(data_dir) / "canonical.sqlite"),
        output_path=output,
        profile_dir=profile_dir,
        allow_destructive_rebuild=args.allow_canonical_rebuild,
    )
    logger.info("export: %s", output)


def cmd_status(args: argparse.Namespace) -> None:
    from metadata_agent.harvest.cursor import CursorStore
    from metadata_agent.store.canonical import CanonicalStore

    config = load_config(args.config)
    data_dir = config.get("storage", {}).get("data_dir", "data")
    profile_dir = _resolve_profile_dir(config)

    cursor_path = Path(data_dir) / "cursors.sqlite"
    if cursor_path.exists():
        cs = CursorStore(cursor_path)
        print("=== Cursors ===")
        for row in cs.list_all():
            print(f"  {row['db']}: watermark={row['watermark']} last_acc={row['last_accession']}")
        cs.close()

    canonical_path = Path(data_dir) / "canonical.sqlite"
    if canonical_path.exists():
        store = CanonicalStore(
            canonical_path,
            profile_dir=profile_dir,
            allow_destructive_rebuild=args.allow_canonical_rebuild,
        )
        count = store.count()
        print(f"\n=== Canonical Store ===\n  Rows: {count}")
        store.close()


def cmd_eval(args: argparse.Namespace) -> None:
    from metadata_agent.extract.golden import run_eval
    from metadata_agent.store.export import export_xlsx

    config = load_config(args.config)
    data_dir = config.get("storage", {}).get("data_dir", "data")
    profile_dir = _resolve_profile_dir(config)

    import tempfile
    tmp_path = Path(tempfile.mktemp(suffix=".xlsx"))
    export_xlsx(
        db_path=str(Path(data_dir) / "canonical.sqlite"),
        output_path=str(tmp_path),
        profile_dir=profile_dir,
        allow_destructive_rebuild=args.allow_canonical_rebuild,
    )

    result = run_eval(predictions_path=str(tmp_path))
    tmp_path.unlink(missing_ok=True)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("pass", False):
        sys.exit(1)


def cmd_align(args: argparse.Namespace) -> None:
    """Re-align existing canonical rows without re-running extraction.

    Only rows whose stored ontology fingerprint differs from the matcher's are
    touched, so a re-run after a completed pass is a no-op. Extraction state is
    never consulted: an ontology release bump must not cost an LLM call.
    """
    from metadata_agent.config.loader import load_profile
    from metadata_agent.config.tiers import tiered_fields_from_config
    from metadata_agent.connectors.http_client import HttpClient
    from metadata_agent.connectors.key_pool import NCBIKeyPool
    from metadata_agent.connectors.rate_limiter import RateLimiterRegistry
    from metadata_agent.store.canonical import (
        PENDING_ONTOLOGY_UNAVAILABLE,
        PRIMARY_KEY,
        CanonicalStore,
    )

    config = load_config(Path(args.config))
    if not _ontology_enabled(config):
        print(
            "error: ontology.enabled is false in config; nothing to align",
            file=sys.stderr,
        )
        sys.exit(2)

    data_dir = config.get("storage", {}).get("data_dir", "data")
    db_path = Path(args.db_path) if args.db_path else Path(data_dir) / "canonical.sqlite"
    if not db_path.is_file():
        # Opening it would create an empty database and report zero stale rows,
        # which reads as "nothing to do" rather than "wrong path".
        print(f"error: no canonical database at {db_path}", file=sys.stderr)
        sys.exit(2)

    profile_dir = _resolve_profile_dir(config, args.profile)
    profile = load_profile(profile_dir, tiered_fields=tiered_fields_from_config(config))
    schema = profile["schema"]

    http = HttpClient(rate_registry=RateLimiterRegistry(), key_pool=NCBIKeyPool())
    matcher = _build_ontology_matcher(config, schema, db_path, http)

    store = CanonicalStore(
        db_path,
        profile_dir=profile_dir,
        allow_destructive_rebuild=args.allow_canonical_rebuild,
    )
    stats = {"stale": 0, "realigned": 0, "rows": 0, "deferred": 0}
    try:
        targets = store.stale_ontology_rows(matcher.fingerprint)
        stats["stale"] = len(targets)
        for row in targets:
            source, source_id = (row[name] for name in PRIMARY_KEY)
            alignments, unreachable = matcher.align_values(row)
            if unreachable:
                store.record_pending(
                    source, source_id, PENDING_ONTOLOGY_UNAVAILABLE,
                    row.get("content_hash"),
                )
                stats["deferred"] += 1
                continue
            store.record_alignment(source, source_id, alignments, matcher.fingerprint)
            stats["realigned"] += 1
            stats["rows"] += len(alignments)
    finally:
        store.close()

    print(json.dumps(stats, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metadata-agent", description="Multi-DB metadata harvesting agent")
    parser.add_argument("--config", "-c", default="config.yaml", help="Config file path")

    sub = parser.add_subparsers(dest="command", required=True)

    p_harvest = sub.add_parser("harvest", help="Harvest metadata from source databases")
    p_harvest.add_argument("--db", help="Specific database to harvest")
    p_harvest.add_argument("--all", action="store_true", dest="harvest_all")
    p_harvest.add_argument("--since", help="Override start date (ISO)")
    p_harvest.add_argument("--until", help="Override end date (ISO)")
    p_harvest.add_argument("--limit", type=int, help="Max accessions per DB")
    p_harvest.add_argument("--dry-run", action="store_true")
    p_harvest.add_argument(
        "--backfill", action="store_true",
        help="Full-history backfill (date-partitioned, resumable); requires --since",
    )
    p_harvest.set_defaults(func=cmd_harvest)

    p_extract = sub.add_parser("extract", help="Extract structured fields from raw metadata")
    p_extract.add_argument("--db", help="Specific database to extract from")
    p_extract.add_argument("--profile", help="Profile name")
    p_extract.add_argument("--with-llm", action="store_true", help="Enable LLM fallback")
    p_extract.add_argument("--llm-api", choices=["auto", "messages", "responses", "chat_completions"],
                           help="LLM protocol (overrides MODEL_API_MODE and llm.api_mode)")
    p_extract.add_argument("--max-llm-calls", type=int, default=100,
                           help="Max LLM request attempts per sample, including its enrichment (default: 100)")
    p_extract.add_argument("--limit", type=int)
    _add_rebuild_flag(p_extract)
    p_extract.set_defaults(func=cmd_extract)

    p_export = sub.add_parser("export", help="Export canonical store to xlsx")
    p_export.add_argument("--output", "-o", help="Output file path")
    _add_rebuild_flag(p_export)
    p_export.set_defaults(func=cmd_export)

    p_status = sub.add_parser("status", help="Show harvest/store status")
    _add_rebuild_flag(p_status)
    p_status.set_defaults(func=cmd_status)

    p_eval = sub.add_parser("eval", help="Run golden-set regression evaluation")
    _add_rebuild_flag(p_eval)
    p_eval.set_defaults(func=cmd_eval)

    p_align = sub.add_parser(
        "align", help="Re-align canonical rows to ontologies without re-extracting"
    )
    p_align.add_argument(
        "db_path", nargs="?", metavar="DB_PATH",
        help="Canonical SQLite path (default: <data_dir>/canonical.sqlite)",
    )
    p_align.add_argument(
        "--ontology-only", action="store_true",
        help="Re-align only; this is the default and only mode (extraction is never re-run)",
    )
    p_align.add_argument("--profile", help="Profile name")
    _add_rebuild_flag(p_align)
    p_align.set_defaults(func=cmd_align)

    from metadata_agent.cli.fetch import add_fetch_parser
    add_fetch_parser(sub)

    from metadata_agent.cli.discover import add_discover_parser
    add_discover_parser(sub)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    _setup_logging(config)
    args.func(args)


if __name__ == "__main__":
    main()
