"""CLI ``fetch`` command — direct accession query with prefix auto-routing.

Accepts one or more accessions, routes each to its connector by prefix, fetches
raw metadata (batched per connector), runs the rules extraction pipeline, and
emits canonical records. Follows a partial-success model: valid accessions that
fetch succeed even when others in the batch fail.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from metadata_agent.config.loader import load_config, load_profile
from metadata_agent.config.tiers import tiered_fields_from_config
from metadata_agent.connectors.accession_router import route_batch
from metadata_agent.connectors.base import SourceConnector, get_connector
from metadata_agent.connectors.http_client import HttpClient
from metadata_agent.connectors.key_pool import NCBIKeyPool
from metadata_agent.connectors.rate_limiter import RateLimiterRegistry
from metadata_agent.enrich import CrossLinkResolver, enrich_record
from metadata_agent.store.source_mapping import connector_to_source

logger = logging.getLogger("metadata_agent")

_MAX_WORKERS = 3

try:
    import rich  # noqa: F401

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


@dataclass
class FetchResult:
    records: dict[str, dict] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _build_http_client() -> HttpClient:
    return HttpClient(rate_registry=RateLimiterRegistry(), key_pool=NCBIKeyPool())


def _build_pipeline(config: dict[str, Any], profile_name: str):
    """Construct a rules-only extraction pipeline for the given profile."""
    from metadata_agent.config.fingerprint import (
        compute_profile_fingerprint,
        compute_prompt_fingerprint,
    )
    from metadata_agent.extract.pipeline import ExtractionPipeline
    from metadata_agent.extract.rules import RulesExtractor, load_rules

    profile_dir = Path("profiles") / profile_name
    tiered_fields = tiered_fields_from_config(config)
    profile = load_profile(profile_dir, tiered_fields=tiered_fields)
    schema = profile["schema"]

    from metadata_agent.config.loader import load_vocab_sets
    enums, aliases = load_vocab_sets(profile_dir, schema)

    rules = load_rules(str(profile_dir / "rules.yaml"))
    rules_extractor = RulesExtractor(rules, schema=schema)
    model_id = config.get("llm", {}).get("model", "claude-sonnet-4-20250514")
    enum_column_names = {c.name for c in schema.columns if c.type in ("enum", "semi")}

    return ExtractionPipeline(
        rules_extractor=rules_extractor,
        llm_extractor=None,
        vocab_sets=enums,
        vocab_aliases=aliases,
        profile_fingerprint=compute_profile_fingerprint(profile_dir),
        prompt_fingerprint=compute_prompt_fingerprint(profile_dir, model_id),
        model_id=model_id,
        enum_columns=enum_column_names,
        schema=schema,
    )


def _connector_for(name: str, config: dict[str, Any], http: HttpClient) -> SourceConnector:
    db_conf = config.get("databases", {}).get(name, {})
    return get_connector(name)(config=db_conf, http_client=http)


def _fetch_group(
    connector_name: str,
    entries: list[tuple[str, str]],
    config: dict[str, Any],
    http: HttpClient,
) -> dict[str, dict]:
    """Fetch one connector's accessions. Returns {accession: raw_record}.

    ``fetch_batch`` keys results by run accession, so it is used only for
    run-level types (SRR/ERR/DRR); experiment/sample/study accessions
    (SRX/SRS/SRP) are fetched individually via ``fetch_by_accession`` to avoid
    losing the requested accession in the batch's run grouping.
    """
    connector = _connector_for(connector_name, config, http)
    out: dict[str, dict] = {}

    use_batch = connector_name == "sra" and hasattr(connector, "fetch_batch")
    run_accs = [acc for acc, acc_type in entries if acc_type == "run"] if use_batch else []
    if run_accs:
        records = connector.fetch_batch(run_accs)
        by_acc = {str(r.get("accession", "")).upper(): r for r in records}
        for acc in run_accs:
            raw = by_acc.get(acc.upper())
            if raw is not None and "error" not in raw.get("raw", {}):
                out[acc] = raw

    fetch = getattr(connector, "fetch_by_accession", connector.fetch)
    for acc, acc_type in entries:
        if use_batch and acc_type == "run":
            continue  # already handled by fetch_batch
        raw = fetch(acc)
        if raw is not None and "error" not in raw.get("raw", {}):
            out[acc] = raw
    return out


def fetch_accessions(
    accessions: list[str],
    config: dict[str, Any],
    profile_name: str,
    progress=None,
) -> FetchResult:
    result = FetchResult()

    grouped, route_errors = route_batch(accessions)
    result.errors.update(route_errors)

    http = _build_http_client()
    raw_by_acc: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_group, name, entries, config, http): name
            for name, entries in grouped.items()
        }
        for fut in futures:
            name = futures[fut]
            try:
                raw_by_acc.update(fut.result())
            except Exception as exc:  # connector-level failure; isolate the group
                logger.error("fetch group %s failed: %s", name, exc)

    # Any routed-but-unfetched accession is a fetch failure.
    for entries in grouped.values():
        for acc, _ in entries:
            if acc not in raw_by_acc:
                result.errors.setdefault(acc, "fetch failed or record not found")

    pipeline = _build_pipeline(config, profile_name)
    # Cross-link enrichment reuses the same HttpClient so sibling fetches share
    # the aggregate NCBI rate limit with the root fetches above.
    resolver = CrossLinkResolver(http, pipeline, databases_config=config.get("databases", {}))
    # Map connector name back per accession to drive source_map extraction.
    conn_of: dict[str, str] = {
        acc: name for name, entries in grouped.items() for acc, _ in entries
    }

    for acc, raw in raw_by_acc.items():
        connector_name = conn_of.get(acc, "")
        try:
            extracted = enrich_record(raw, connector_name, pipeline, resolver)
        except Exception as exc:
            result.errors[acc] = f"extraction failed: {exc}"
            continue
        if extracted is None:
            result.errors[acc] = "extraction produced no record"
            continue
        # Identity is forced from the root/primary record; enrichment only fills.
        extracted["Source"] = connector_to_source(connector_name)
        extracted["SourceID"] = acc
        # Drop pipeline-internal bookkeeping; expose only canonical columns.
        for meta in ("_audit_log", "_diagnostics", "_status", "_ontology_alignments",
                     "content_hash", "profile_fingerprint", "ontology_fingerprint"):
            extracted.pop(meta, None)
        result.records[acc] = extracted
        if progress is not None:
            progress(acc)

    return result


# --- output rendering --------------------------------------------------------

_DISPLAY_COLUMNS = ["SourceID", "Source", "OrganismSimple", "Class", "Tissue"]


def _render_json(result: FetchResult) -> str:
    return json.dumps(list(result.records.values()), ensure_ascii=False, indent=2)


def _render_table(result: FetchResult) -> str:
    if not _HAS_RICH:
        header = "\t".join(_DISPLAY_COLUMNS)
        rows = [
            "\t".join(str(rec.get(c, "")) for c in _DISPLAY_COLUMNS)
            for rec in result.records.values()
        ]
        return "\n".join([header, *rows])

    from rich.console import Console
    from rich.table import Table

    table = Table()
    for col in _DISPLAY_COLUMNS:
        table.add_column(col)
    for rec in result.records.values():
        table.add_row(*[str(rec.get(c, "")) for c in _DISPLAY_COLUMNS])
    console = Console(file=sys.stdout)
    with console.capture() as cap:
        console.print(table)
    return cap.get()


def _resolve_format(fmt: str | None, is_tty: bool) -> str:
    if fmt:
        return fmt
    return "table" if is_tty else "json"


def add_fetch_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("fetch", help="Fetch metadata by accession (auto-routed by prefix)")
    p.add_argument("accessions", nargs="+", help="Accessions or h5ad filenames/paths containing one accession")
    p.add_argument("--format", choices=["json", "table"], help="Output format (default: table if TTY else json)")
    p.add_argument("--output", "-o", help="Write output to FILE instead of stdout")
    p.add_argument("--profile", help="Profile name (default: active_profile)")
    p.set_defaults(func=cmd_fetch)


def cmd_fetch(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    profile_name = args.profile or config.get("active_profile", "human_cellline")

    fmt = _resolve_format(args.format, sys.stdout.isatty())

    progress_cb, finish = _make_progress(len(args.accessions))
    result = fetch_accessions(args.accessions, config, profile_name, progress=progress_cb)
    finish()

    rendered = _render_json(result) if fmt == "json" else _render_table(result)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(rendered)
    else:
        print(rendered)

    print(
        f"Fetched {len(result.records)}/{len(args.accessions)} accessions "
        f"({len(result.errors)} errors)",
        file=sys.stderr,
    )
    for acc, err in result.errors.items():
        print(f"  {acc}: {err}", file=sys.stderr)

    sys.exit(0 if result.records else 1)


def _make_progress(total: int):
    """Return (per-item callback, finish callback). Uses rich if available."""
    if not _HAS_RICH:
        done = {"n": 0}

        def cb(_acc: str) -> None:
            done["n"] += 1
            print(f"\rFetching... {done['n']}/{total}", end="", file=sys.stderr)

        def finish() -> None:
            print("", file=sys.stderr)

        return cb, finish

    from rich.progress import Progress

    progress = Progress(transient=True)
    task = progress.add_task("Fetching", total=total)
    progress.start()

    def cb(_acc: str) -> None:
        progress.advance(task)

    def finish() -> None:
        progress.stop()

    return cb, finish
