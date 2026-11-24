"""Cross-link sibling resolver — the enrichment engine.

Given an already-fetched root raw record, discovers sibling accessions in other
databases (GEO ``Sample_relation``, SRA/ENA identifiers + ELINK, BioSample
``_links``), fetches them through the shared ``HttpClient``, extracts each with
its **own** connector rules, and returns them for a field-level merge.

Traversal is BioSample-centered and bounded: at most ``max_edges`` (2) graph
edges (``GSM → SAMN → SRS/ERS``), visited ``(connector, accession)`` keys are
deduplicated, fan-out is capped at ``max_siblings``, and a sibling is fetched
only while at least one configured target field is still fillable (field-trigger
gate). Living outside ``ExtractionPipeline`` and the connectors avoids an import
cycle between connectors, router, store, and extraction.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from metadata_agent.connectors.accession_router import route_accession
from metadata_agent.connectors.base import get_connector
from metadata_agent.connectors.ngdc_biosample import current_samc_snapshot
from metadata_agent.enrich.bundle import content_hash as bundle_content_hash
from metadata_agent.enrich.merge import is_fillable, merge_enrichment, sibling_priority
from metadata_agent.runtime import count, timed
from metadata_agent.store.canonical import defers_canonical_write
from metadata_agent.store.source_cache import SourceCache

logger = logging.getLogger(__name__)

# Fields whose emptiness justifies following cross-links (the golden-口径 fields
# most often missing from a single record).
DEFAULT_TARGET_FIELDS = [
    "Class", "Tissue", "Tumor/Cancer", "Gender", "Time", "Organism",
]

# Sibling accession types worth following (sample/experiment level, not runs or
# studies), matching route_accession's accession_type.
_SIBLING_TYPES = {"sample", "experiment"}

_ACC_RE = re.compile(
    r"\b([SED]R[RXSP]\d{6,10}|SAM[NEDC][A-Z]?\d{6,10}|GS[EM]\d{4,10}|CR[RA]\d{6,10})\b"
)


@dataclass(frozen=True)
class SiblingRef:
    connector: str
    accession: str
    relation: str
    evidence: str


@dataclass
class EnrichmentResult:
    siblings: list[tuple[SiblingRef, dict[str, Any]]] = field(default_factory=list)
    sibling_raws: list[dict[str, Any]] = field(default_factory=list)


class CrossLinkResolver:
    def __init__(
        self,
        http: Any,
        pipeline: Any,
        *,
        databases_config: dict[str, Any] | None = None,
        max_siblings: int = 8,
        max_edges: int = 2,
        target_fields: list[str] | None = None,
        source_cache: SourceCache | None = None,
    ):
        self._http = http
        self._pipeline = pipeline
        self._databases_config = databases_config or {}
        self._max_siblings = max_siblings
        self._max_edges = max_edges
        self._target_fields = target_fields or DEFAULT_TARGET_FIELDS
        self._cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._source_cache = source_cache

    def gather(
        self, root_raw: dict[str, Any], root_connector: str, primary_canonical: dict[str, Any]
    ) -> EnrichmentResult:
        """Discover, fetch, and extract siblings for one root record."""
        pending = {f for f in self._target_fields if is_fillable(primary_canonical.get(f))}
        if not pending:
            return EnrichmentResult()  # field-trigger: nothing missing → no fetches

        root_acc = str(root_raw.get("accession", "")).upper()
        visited: set[tuple[str, str]] = {(root_connector, root_acc)}
        result = EnrichmentResult()

        frontier = self._discover_refs(root_raw.get("raw", root_raw), root_connector)
        edge = 1
        while frontier and edge <= self._max_edges and len(result.siblings) < self._max_siblings:
            next_frontier: list[SiblingRef] = []
            for ref in frontier:
                if len(result.siblings) >= self._max_siblings:
                    break
                key = (ref.connector, ref.accession)
                if key in visited:
                    continue
                visited.add(key)

                raw = self._fetch(ref)
                if raw is None:
                    continue
                canonical = self._extract(raw, ref.connector)
                if canonical is None:
                    continue

                result.siblings.append((ref, canonical))
                result.sibling_raws.append(raw)
                for f in list(pending):
                    if not is_fillable(canonical.get(f)):
                        pending.discard(f)
                if not pending:
                    return result  # every target filled — stop early

                # Only a BioSample hub expands to the next edge (GSM→SAMN→SRS).
                # SAMC (NGDC BioSample) routes to the GSA connector but is still a
                # hub, so key on the accession being a BioSample, not the connector.
                if edge < self._max_edges and ref.accession.startswith("SAM"):
                    next_frontier.extend(self._discover_refs(raw.get("raw", raw), ref.connector))

            frontier = next_frontier
            edge += 1

        return result

    def _discover_refs(self, raw_dict: Any, evidence: str) -> list[SiblingRef]:
        text = json.dumps(raw_dict, ensure_ascii=False, default=str)
        refs: list[SiblingRef] = []
        seen: set[tuple[str, str]] = set()
        for token in _ACC_RE.findall(text):
            norm = token.upper()
            try:
                connector, acc_type = route_accession(norm)
            except ValueError:
                continue
            if acc_type not in _SIBLING_TYPES:
                continue
            key = (connector, norm)
            if key in seen:
                continue
            seen.add(key)
            refs.append(SiblingRef(connector=connector, accession=norm, relation=acc_type, evidence=evidence))
        # BioSample hubs first so the 2-edge traversal stays BioSample-centered.
        refs.sort(key=lambda r: (sibling_priority(r.connector, r.relation), r.accession))
        return refs

    def _fetch(self, ref: SiblingRef) -> dict[str, Any] | None:
        key = (ref.connector, ref.accession)
        if self._source_cache is None and key in self._cache:
            count("source_cache_hits")
            return self._cache[key]
        if self._source_cache is not None:
            raw = self._source_cache.get_or_load(
                "sibling_raw_v1", key, lambda: self._fetch_uncached(ref),
                valid=lambda value: isinstance(value, dict) and isinstance(value.get("raw"), dict)
                and bool(value["raw"]) and not value["raw"].get("error")
                and (not ref.accession.startswith("SAMC") or current_samc_snapshot(value)),
            )
        else:
            raw = self._fetch_uncached(ref)
        # A transient failure must remain retryable, including later samples.
        if raw is not None and self._source_cache is None:
            self._cache[key] = raw
        return raw

    def _fetch_uncached(self, ref: SiblingRef) -> dict[str, Any] | None:
        raw: dict[str, Any] | None = None
        try:
            db_conf = self._databases_config.get(ref.connector, {})
            connector = get_connector(ref.connector)(config=db_conf, http_client=self._http)
            fetch = getattr(connector, "fetch_by_accession", connector.fetch)
            with timed("sibling_fetch"):
                fetched = fetch(ref.accession)
            if fetched is not None and "error" not in fetched.get("raw", {}):
                raw = fetched
        except Exception as exc:
            logger.warning("enrichment sibling fetch failed for %s:%s: %s", ref.connector, ref.accession, exc)
        return raw

    def _extract(self, raw: dict[str, Any], connector: str) -> dict[str, Any] | None:
        """Extract a sibling up to, but not including, finalization.

        Siblings feed a field-level merge, never the store, so finalizing each
        one would align its text against the ontology under an identity that is
        never upserted, and bucket its out-of-vocab enum values to ``other`` —
        the merge would then carry the replacement instead of what the sibling
        actually said. The merged record is finalized once, in ``enrich_record``.
        """
        try:
            extracted = self._pipeline._extract_one_pre_validation(
                raw, connector_id=connector
            )
        except Exception as exc:
            logger.warning("enrichment sibling extract failed for %s: %s", connector, exc)
            return None
        return extracted[0] if extracted is not None else None


def enrich_record(
    root_raw: dict[str, Any],
    connector: str,
    pipeline: Any,
    resolver: CrossLinkResolver,
    *,
    stored_hash: str | None = None,
    stored_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    """Extract a root record and field-fill it from cross-link siblings.

    The stored content hash is computed over the primary+siblings bundle so a
    sibling change re-triggers extraction while sibling discovery order does not.
    Returns ``None`` when ``should_skip`` says the bundle is unchanged.

    New pipeline order (T9):
    1. Extract primary (rules+LLM, no validation yet)
    2. Gather and merge siblings (enrichment)
    3. Finalize merged record (tiered acquisition + coercion + vocab validation)
    """
    # Public extraction entry points own the sample boundary. Sibling calls
    # inside gather must not reset the root's model allowance.
    with getattr(pipeline, "sample_scope", nullcontext)():
        return _enrich_scoped(root_raw, connector, pipeline, resolver, stored_hash, stored_fingerprint)


def _enrich_scoped(root_raw, connector, pipeline, resolver, stored_hash, stored_fingerprint):
    # Extract primary without validation
    with timed("primary"):
        result = pipeline._extract_one_pre_validation(root_raw, connector_id=connector)
    if result is None:
        return None
    primary, primary_hash = result

    if defers_canonical_write(primary.get("_status")):
        # Incomplete primary reviews cannot commit. Do not spend sibling fetch
        # or model calls on a row that must be retried in a later pass.
        return pipeline._finalize_one(primary, primary_hash, root_raw, connector)

    # Gather siblings and compute bundle hash
    with timed("enrichment"):
        enrichment_result = resolver.gather(root_raw, connector, primary)
    hash_ = bundle_content_hash(root_raw.get("raw", root_raw), enrichment_result.sibling_raws)
    if stored_hash is not None and pipeline.should_skip(hash_, stored_hash, stored_fingerprint):
        return None

    # Merge enrichment before validation
    diagnostics = list(primary.get("_diagnostics") or [])
    filled = merge_enrichment(primary, enrichment_result.siblings, diagnostics=diagnostics)
    if diagnostics:
        filled["_diagnostics"] = diagnostics

    # Finalize: tiered acquisition + numeric coercion + vocab validation
    finalized = pipeline._finalize_one(filled, hash_, root_raw, connector)
    return finalized
