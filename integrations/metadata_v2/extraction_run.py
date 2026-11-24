"""Harvest and extract public accession metadata using the v2 pipeline."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from metadata_agent.config.loader import load_profile
from metadata_agent.config.tiers import tiered_fields_from_config
from metadata_agent.config.fingerprint import (
    compute_profile_fingerprint,
    compute_prompt_fingerprint,
    with_llm_transport,
)
from metadata_agent.config.llm import resolve_llm_settings
from metadata_agent.connectors.base import get_connector

# Import connector modules so they register themselves with the registry.
import metadata_agent.connectors.arrayexpress  # noqa: F401
import metadata_agent.connectors.biosample  # noqa: F401
import metadata_agent.connectors.ena  # noqa: F401
import metadata_agent.connectors.geo  # noqa: F401
import metadata_agent.connectors.gsa  # noqa: F401
import metadata_agent.connectors.sra  # noqa: F401
from metadata_agent.connectors.http_client import HttpClient
from metadata_agent.connectors.key_pool import NCBIKeyPool
from metadata_agent.connectors.ngdc_biosample import BIOSAMPLE_BROWSE_URL, current_samc_snapshot
from metadata_agent.connectors.rate_limiter import RateLimiterRegistry
from metadata_agent.enrich import CrossLinkResolver, enrich_record
from metadata_agent.extract.fanout import fanout_samples
from metadata_agent.extract.llm import LLMExtractor
from metadata_agent.extract.parallel import parallel_process
from metadata_agent.extract.pipeline import ExtractionPipeline
from metadata_agent.extract.rules import RulesExtractor, load_rules
from metadata_agent.extract.tiers import TierExtractor
from metadata_agent.runtime import KeyedLocks, collect_metrics
from metadata_agent.store.canonical import CanonicalStore
from metadata_agent.store.llm_cache import LLMCache
from metadata_agent.store.raw import RawStore
from metadata_agent.store.source_mapping import connector_to_source
from metadata_agent.store.source_cache import SourceCache

logger = logging.getLogger(__name__)
DATA_DIR = Path("outputs/metadata_v2")
PROFILE_DIR = Path(__file__).parent / "profiles" / "human_cellline"
RUN_CONFIG: dict = {}


# Connector-specific config passed at construction time. fetch() ignores these
# for most connectors, but search() reads them, so keep sensible defaults.
CONNECTOR_CONFIG: dict[str, dict] = {
    "geo": {"search_query": "", "date_field": "MDAT"},
    "ena": {"search_query": "", "date_field": "last_updated"},
    "sra": {"search_query": "", "date_field": "MDAT"},
    "gsa": {"search_query": ""},
    "arrayexpress": {"search_query": "", "date_field": "release_date"},
    "biosample": {"search_query": ""},
}


@dataclass(frozen=True)
class _RunResources:
    cancelled: Event
    request_locks: KeyedLocks
    source_cache: SourceCache
    rate_registry: RateLimiterRegistry
    key_pool: NCBIKeyPool


@dataclass
class _SampleJob:
    connector: str
    source: str
    accession: str
    raw: dict
    series_id: str | None
    stored_hash: str | None
    stored_fingerprint: str | None


@dataclass
class _SampleResult:
    job: _SampleJob
    record: dict | None
    error: Exception | None
    metrics: dict[str, float]


def _build_connector(name: str, http_client: HttpClient):
    connector_cls = get_connector(name)
    config = CONNECTOR_CONFIG.get(name, {})
    return connector_cls(config=config, http_client=http_client)


def _sample_address(source_connector: str, accession: str) -> str:
    if accession.startswith(("https://", "http://")):
        return accession
    if source_connector == "geo":
        return f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}"
    if source_connector in ("ena", "sra"):
        return f"https://www.ebi.ac.uk/ena/browser/view/{accession}"
    if source_connector == "gsa":
        if accession.startswith("SAMC"):
            return f"{BIOSAMPLE_BROWSE_URL}/{accession}"
        return f"https://ngdc.cncb.ac.cn/gsa/browse/{accession}"
    if source_connector == "arrayexpress":
        return f"https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{accession}"
    if source_connector == "biosample":
        return f"https://www.ncbi.nlm.nih.gov/biosample/{accession}"
    return ""


def step_harvest(grouped: dict[str, list[tuple[str, str]]], *, resume: bool = False) -> None:
    """Fetch requested raws; resume keeps valid snapshots and fills only gaps."""
    total = sum(len(v) for v in grouped.values())
    logger.info(
        "=== STEP 1: Harvest %d accessions across %d connector(s): %s ===",
        total, len(grouped), ", ".join(sorted(grouped)),
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rate_registry = RateLimiterRegistry()
    key_pool = NCBIKeyPool()
    http_client = HttpClient(rate_registry=rate_registry, key_pool=key_pool)

    for connector_name, entries in grouped.items():
        logger.info(
            "--- Harvesting %d accessions from %s ---", len(entries), connector_name
        )
        connector = _build_connector(connector_name, http_client)
        raw_store = RawStore(db_name=connector_name, data_dir=str(DATA_DIR))

        failed = 0
        reused = 0
        for i, (acc, _acc_type) in enumerate(entries, 1):
            if resume:
                cached = raw_store.get(acc)
                needs_samc_refresh = connector_name == "gsa" and acc.startswith("SAMC") and (
                    cached is not None and not current_samc_snapshot(cached)
                )
                if (cached is not None and cached.get("raw") and not cached["raw"].get("error")
                        and not needs_samc_refresh):
                    reused += 1
                    continue
            if i == 1 or i % 100 == 0 or i == len(entries):
                logger.info("  [%s %d/%d] %s ...", connector_name, i, len(entries), acc)
            try:
                raw = connector.fetch(acc)
            except Exception as exc:  # network / parse errors shouldn't abort the run
                logger.warning("    FAILED %s: %s", acc, exc)
                failed += 1
                continue
            if raw.get("raw", {}).get("error"):
                logger.warning("    FAILED %s: %s", acc, raw["raw"]["error"])
                failed += 1
                continue
            raw_store.upsert(raw)

        count = raw_store.count()
        raw_store.close()
        logger.info(
            "  %s raw store now has %d records (%d reused, %d failed)",
            connector_name, count, reused, failed,
        )


def _build_pipeline(
    with_llm: bool,
    max_llm_calls: int,
    http_client: HttpClient,
    allow_canonical_rebuild: bool = False,
    api_mode: str | None = None,
    *,
    runtime: _RunResources | None = None,
    create_canonical: bool = True,
):
    """Construct the shared extraction pipeline plus its stores.

    ``http_client`` is the caller's, so tier acquisition and sibling enrichment
    share one per-host rate limit budget, as ``cli/main.py`` does.
    """
    config = RUN_CONFIG
    tiered_fields = tiered_fields_from_config(config)
    profile = load_profile(PROFILE_DIR, tiered_fields=tiered_fields)
    schema = profile["schema"]

    from metadata_agent.config.loader import load_vocab_sets
    enums, aliases = load_vocab_sets(PROFILE_DIR, schema)

    rules = load_rules(str(PROFILE_DIR / "rules.yaml"))
    rules_extractor = RulesExtractor(rules, schema=schema)

    settings = resolve_llm_settings(config, api_mode=api_mode) if with_llm else None
    model_id = settings.model if settings else ""
    profile_fp = compute_profile_fingerprint(PROFILE_DIR)
    prompt_fp = compute_prompt_fingerprint(PROFILE_DIR, model_id)
    legacy_resume_fp = profile_fp if settings and settings.api_mode == "messages" else None

    llm_extractor = None
    if with_llm:
        profile_fp = with_llm_transport(profile_fp, settings.cache_identity)
        prompt_fp = with_llm_transport(prompt_fp, settings.cache_identity)
        logger.info("LLM transport: %s, model: %s", settings.api_mode, model_id)
        llm_extractor = LLMExtractor(
            profile_dir=str(PROFILE_DIR),
            max_calls=max_llm_calls,
            model=model_id,
            base_url=settings.base_url,
            api_mode=settings.api_mode,
        )

    canonical = CanonicalStore(
        DATA_DIR / "canonical.sqlite",
        profile_dir=PROFILE_DIR,
        allow_destructive_rebuild=allow_canonical_rebuild,
    ) if create_canonical else None
    llm_cache = LLMCache(DATA_DIR / "canonical.sqlite")

    enum_column_names = {col.name for col in schema.columns if col.type in ("enum", "semi")}
    trigger_fields = [col.name for col in schema.columns if col.type in ("enum", "semi") and not col.system]

    pipeline = ExtractionPipeline(
        rules_extractor=rules_extractor,
        llm_extractor=llm_extractor,
        vocab_sets=enums,
        vocab_aliases=aliases,
        max_llm_calls=max_llm_calls,
        profile_fingerprint=profile_fp,
        prompt_fingerprint=prompt_fp,
        model_id=model_id,
        llm_cache=llm_cache,
        enum_columns=enum_column_names,
        trigger_fields=trigger_fields,
        schema=schema,
        tier_extractor=TierExtractor(config, schema, http_client,
                                     source_cache=runtime.source_cache if runtime else None),
        ontology_matcher=_demo_ontology_matcher(config, schema, http_client),
        legacy_resume_fingerprint=legacy_resume_fp,
        cancel_event=runtime.cancelled if runtime else None,
        request_locks=runtime.request_locks if runtime else None,
    )
    return pipeline, canonical, llm_cache


def _demo_ontology_matcher(config, schema, http_client):
    from metadata_agent.cli.main import _build_ontology_matcher

    return _build_ontology_matcher(config, schema, DATA_DIR / "canonical.sqlite", http_client)


def _extract_series_id(row: dict) -> str | None:
    raw = row.get("raw", {})
    if isinstance(raw, dict):
        series = raw.get("Sample_series_id")
        if isinstance(series, list) and series:
            return series[0]
        if isinstance(series, str):
            return series
    return None


def _requested_raw_rows(
    raw_store: RawStore, entries: list[tuple[str, str]], connector_name: str
) -> list[dict]:
    """The harvested rows for exactly the accessions this run asked for.

    The raw stores are persistent, so scanning them whole would extract and
    report on every record any earlier run ever harvested. A requested
    accession that is absent is reported rather than passed over: with
    ``--skip-harvest`` that is the difference between an empty report and a
    silent one.
    """
    rows = []
    for accession, _accession_type in entries:
        row = raw_store.get(accession)
        if row is None:
            logger.warning(
                "  %s: no harvested record for %s — skipping", connector_name, accession
            )
            continue
        rows.append(row)
    return rows


def _sample_jobs(grouped, canonical, pipeline, resume, stats):
    jobs = []
    seen = set()
    for connector in sorted(grouped):
        source = connector_to_source(connector)
        with closing(RawStore(db_name=connector, data_dir=str(DATA_DIR))) as raw_store:
            for row in _requested_raw_rows(raw_store, grouped[connector], connector):
                if (connector == "gsa" and row.get("accession", "").startswith("SAMC")
                        and not current_samc_snapshot(row)):
                    logger.warning("  %s: outdated/invalid SAMC snapshot; harvest again without --skip-harvest",
                                   row["accession"])
                    stats["failed"] += 1
                    continue
                for accession, raw in fanout_samples(row, connector):
                    if not accession:
                        continue
                    identity = (source, accession)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    # Older SAMC rows were extracted from the wrong endpoint.
                    # Their missing/wrong address distinguishes them without
                    # invalidating completed GEO/ENA rows in the same database.
                    old_samc = False
                    if connector == "gsa" and accession.startswith("SAMC"):
                        existing = canonical.get(source, accession)
                        old_samc = existing is not None and existing.get("Address") != raw["raw"].get("sample_url")
                    if resume and not old_samc and any(canonical.is_extraction_complete(source, accession, fingerprint)
                                      for fingerprint in pipeline.resume_fingerprints):
                        stats["skipped"] += 1
                        continue
                    jobs.append(_SampleJob(
                        connector, source, accession, raw, _extract_series_id(row),
                        None if resume or old_samc else canonical.get_content_hash(source, accession),
                        None if resume else canonical.get_profile_fingerprint(source, accession),
                    ))
    return jobs


def _process_sample(job, pipeline, resolver, resume):
    logger.info("  %s %s: processing", "Resume" if resume else "Extract", job.accession)
    record = error = None
    with collect_metrics() as metrics:
        try:
            kwargs = {"stored_hash": job.stored_hash, "stored_fingerprint": job.stored_fingerprint}
            if resolver is not None:
                record = enrich_record(job.raw, job.connector, pipeline, resolver, **kwargs)
            else:
                record = pipeline.extract_one(job.raw, connector_id=job.connector, **kwargs)
            if record is not None:
                record["Source"], record["SourceID"] = job.source, job.accession
                if job.series_id and not record.get("SeriesID"):
                    record["SeriesID"] = job.series_id
                if record.get("Address"):
                    record["Address"] = _sample_address(job.connector, record["Address"])
        except Exception as exc:
            error = exc
    return _SampleResult(job, record, error, dict(metrics))


def _mark_failed(canonical, job, reason):
    # An older completed row may exist when refreshing a sample. Preserve it,
    # but ensure resume retries the failed update rather than skipping it.
    try:
        canonical.record_pending(job.source, job.accession, reason)
    except Exception:
        logger.exception("  Could not save pending status for %s", job.accession)


def step_extract(
    grouped: dict[str, list[tuple[str, str]]],
    with_llm: bool = False,
    max_llm_calls: int = 20,
    enrich: bool = False,
    allow_canonical_rebuild: bool = False,
    resume: bool = False,
    llm_api: str | None = None,
    workers: int = 1,
    source_cache_ttl: float = 24.0,
    refresh_source_cache: bool = False,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Extract the requested accessions from each connector's raw store.

    When ``enrich`` is set, each sample root is passed through the cross-link
    resolver first, so sibling records (BioSample hub → INSDC sample / ENA
    mirror) field-fill empty canonical columns before extraction is committed.
    Each worker owns its HTTP and model clients. Sibling fetches, tier
    acquisition and ontology lookups share rate limiters across workers so
    aggregate host traffic stays within the connector rate limits.

    Returns the identities whose canonical row this run wrote, and the run
    stats. Deferred and skipped samples are excluded: neither wrote or updated
    a canonical row, so counting them would report a fill rate against rows the
    run did not produce.

    Resume explicitly reuses completed snapshots from the same profile before
    any extraction/enrichment. Keep the original model/enrichment options;
    omit resume when intentionally refreshing sources or changing run settings.
    """
    if not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    if max_llm_calls < 0:
        raise ValueError("Per-sample model call limit must be nonnegative")
    started = time.perf_counter()
    logger.info("=== STEP 2: Extract from %s (llm=%s enrich=%s workers=%d) ===",
                ", ".join(sorted(grouped)), with_llm, enrich, workers)
    stats = {"extracted": 0, "skipped": 0, "deferred": 0, "failed": 0, "enriched": 0}
    written: list[tuple[str, str]] = []
    totals: dict[str, float] = defaultdict(float)
    with ExitStack() as resources:
        source_cache = resources.enter_context(closing(SourceCache(
            DATA_DIR / "source_cache.sqlite", source_cache_ttl * 3600, refresh=refresh_source_cache,
        )))
        runtime = _RunResources(Event(), KeyedLocks(), source_cache,
                                RateLimiterRegistry(), NCBIKeyPool())
        http = resources.enter_context(closing(HttpClient(runtime.rate_registry, runtime.key_pool)))
        pipeline, canonical, llm_cache = _build_pipeline(
            with_llm, max_llm_calls, http, allow_canonical_rebuild, api_mode=llm_api, runtime=runtime,
        )
        resources.callback(canonical.close)
        resources.callback(llm_cache.close)
        resources.callback(pipeline.close)
        jobs = _sample_jobs(grouped, canonical, pipeline, resume, stats)
        logger.info("  Step 2 queue: %d unfinished samples, %d completed samples skipped", len(jobs), stats["skipped"])

        def make_resolver(worker_http, worker_pipeline):
            return CrossLinkResolver(worker_http, worker_pipeline, databases_config=CONNECTOR_CONFIG,
                                     source_cache=source_cache) if enrich else None

        @contextmanager
        def worker_factory():
            with ExitStack() as worker_resources:
                worker_http = worker_resources.enter_context(closing(
                    HttpClient(runtime.rate_registry, runtime.key_pool),
                ))
                worker_pipeline, _, worker_cache = _build_pipeline(
                    with_llm, max_llm_calls, worker_http, api_mode=llm_api,
                    runtime=runtime, create_canonical=False,
                )
                worker_resources.callback(worker_cache.close)
                worker_resources.callback(worker_pipeline.close)
                resolver = make_resolver(worker_http, worker_pipeline)
                yield lambda job: _process_sample(job, worker_pipeline, resolver, resume)

        if workers == 1:
            resolver = make_resolver(http, pipeline)
            results = (_process_sample(job, pipeline, resolver, resume) for job in jobs)
        else:
            results = parallel_process(jobs, worker_factory, workers, on_cancel=runtime.cancelled.set)
        try:
            with closing(results):
                for completed, outcome in enumerate(results, 1):
                    job, record, error = outcome.job, outcome.record, outcome.error
                    metrics = outcome.metrics
                    if error is not None:
                        logger.error("  Extract failed for %s: %s", job.accession, error,
                                     exc_info=(type(error), error, error.__traceback__))
                        stats["failed"] += 1
                        _mark_failed(canonical, job, "extraction_failed")
                    elif record is None:
                        stats["skipped"] += 1
                    else:
                        if enrich and record.get("_diagnostics"):
                            stats["enriched"] += 1
                        write_started = time.perf_counter()
                        try:
                            # Only the caller thread writes the canonical store.
                            committed = canonical.upsert(record)
                            stats["extracted" if committed else "deferred"] += 1
                            if committed:
                                written.append((job.source, job.accession))
                        except Exception as exc:
                            logger.exception("  Upsert failed for %s: %s", job.accession, exc)
                            stats["failed"] += 1
                            _mark_failed(canonical, job, "canonical_write_failed")
                        finally:
                            metrics["write"] = time.perf_counter() - write_started
                    for name, value in metrics.items():
                        totals[name] += value
                    logger.info(
                        "  Timing %s [%d/%d]: total=%.2fs primary=%.2fs enrichment=%.2fs tiers=%.2fs "
                        "ontology=%.2fs write=%.3fs; llm=%.2fs sibling_fetch=%.2fs ena=%.2fs pubmed=%.2fs "
                        "sample_calls=%d/%d llm_cache_hits=%d source_cache_hits=%d",
                        job.accession, completed, len(jobs), metrics.get("total", 0) + metrics.get("write", 0),
                        metrics.get("primary", 0), metrics.get("enrichment", 0), metrics.get("tiers", 0),
                        metrics.get("ontology", 0), metrics.get("write", 0), metrics.get("llm", 0),
                        metrics.get("sibling_fetch", 0), metrics.get("ena", 0), metrics.get("pubmed", 0),
                        metrics.get("llm_calls", 0), max_llm_calls, metrics.get("llm_cache_hits", 0),
                        metrics.get("source_cache_hits", 0),
                    )
        except KeyboardInterrupt:
            logger.warning("Interrupted: committed samples and caches are saved; continue with --resume")
            raise
        logger.info("  Extraction stats: %s (total LLM calls: %d; per-sample limit: %d)",
                    stats, totals["llm_calls"], max_llm_calls)
    logger.info("  Step 2 timing: wall=%.2fs workers=%d llm_cache_hits=%d source_cache_hits=%d",
                time.perf_counter() - started, workers, totals["llm_cache_hits"], totals["source_cache_hits"])
    # Stable report order despite completion-order writes.
    completed_ids = set(written)
    written = [(job.source, job.accession) for job in jobs if (job.source, job.accession) in completed_ids]
    return written, stats
