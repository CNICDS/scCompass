"""Ontology term matching via OLS4 API with fuzzy scoring and caching."""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from metadata_agent.config.loader import ProfileSchema
from metadata_agent.store.canonical import PENDING_ONTOLOGY_UNAVAILABLE

logger = logging.getLogger(__name__)

# Names the ontology fingerprint is built from. They version the *decisions* a
# stored alignment row embodies, so a change to either invalidates prior rows.
MATCHER_VERSION = "ols4-sequencematcher-v1"
NORMALIZATION_VERSION = "NFKC-casefold-v1"

# Fuzzy matching parameters
FUZZY_THRESHOLD = 0.90
AMBIGUITY_TOLERANCE = 0.02

OLS4_SEARCH_URL = "https://www.ebi.ac.uk/ols4/api/search"

# Field → ontology mapping for the 7 aligned fields. The identifiers are the
# ones the spec stores and joins on; OLS4's query parameter wants them
# lowercased, which is done at the request rather than kept as a second spelling.
FIELD_ONTOLOGY_MAP = {
    "Tissue": "UBERON",
    "AnatomicalSite": "UBERON",
    "CellSource/CellSorting": "CL",
    "Organoid/Inducible_begin": "CL",
    "Organoid/Inducible_end": "CL",
    "Disease": "DOID",
    "DiseaseSubtype": "DOID",
}


def _required_ontologies() -> list[str]:
    return sorted(set(FIELD_ONTOLOGY_MAP.values()))


def compute_ontology_fingerprint(
    schema: ProfileSchema, ontology_versions: dict[str, str]
) -> str:
    """Fingerprint everything that can change an alignment result.

    A row whose fingerprint still matches needs no re-alignment; a row whose
    fingerprint differs needs alignment *only*, which is why this is kept apart
    from ``profile_fingerprint``. Delimiters are read from the schema because
    they decide how a value splits into parts, and the part is what gets
    matched.
    """
    payload = {
        "ontology_versions": {
            ontology: ontology_versions.get(ontology)
            for ontology in _required_ontologies()
        },
        "matcher_version": MATCHER_VERSION,
        "normalization": NORMALIZATION_VERSION,
        "threshold": FUZZY_THRESHOLD,
        "tolerance": AMBIGUITY_TOLERANCE,
        "field_ontology_map": dict(FIELD_ONTOLOGY_MAP),
        "field_delimiters": _field_delimiters(schema),
        "ols_base_url": OLS4_SEARCH_URL,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _field_delimiters(schema: ProfileSchema) -> dict[str, str | None]:
    """The delimiter each aligned field declares; ``None`` when it declares none.

    Every aligned field appears, present in the schema or not, so dropping one
    changes its entry rather than the payload's shape.
    """
    by_name = {col.name: col for col in schema.columns}
    return {
        name: (by_name[name].delimiter if name in by_name else None)
        for name in FIELD_ONTOLOGY_MAP
    }


class OntologyMatcher:
    """Ontology alignment manager with OLS4 API integration and caching.

    The HTTP client is required rather than optional: a matcher that quietly
    declines to align is indistinguishable from an ontology that returned no
    terms. Callers that mean "alignment is off" pass no matcher at all.
    """

    def __init__(
        self,
        schema: ProfileSchema,
        ontology_versions: dict[str, str],
        cache_db: Path,
        http_client: Any,
    ):
        self.schema = schema
        self.ontology_versions = ontology_versions
        self.cache_db = cache_db
        self.http_client = http_client
        self._validate_versions()
        self._validate_aligned_column_types()
        self._fingerprint = compute_ontology_fingerprint(schema, ontology_versions)

    @property
    def fingerprint(self) -> str:
        """The ontology fingerprint a row aligned by this matcher carries."""
        return self._fingerprint

    def _validate_versions(self) -> None:
        """Ensure every ontology the field map needs is explicitly pinned."""
        for ontology_key in _required_ontologies():
            version = self.ontology_versions.get(ontology_key)
            if not version or version == "latest":
                raise ValueError(
                    f"Ontology version for {ontology_key} must be explicitly pinned"
                )

    def _validate_aligned_column_types(self) -> None:
        """Refuse a schema whose aligned columns cannot keep their original text.

        Alignment records the text it matched on, and re-alignment is specified
        to re-read it from the canonical row, so those columns must still hold
        what the source said. Vocabulary validation rewrites an out-of-vocab
        ``enum`` value to ``other``/``unknown``; ``semi`` and plain text columns
        are left alone.
        """
        by_name = {col.name: col for col in self.schema.columns}
        enum_aligned = sorted(
            name
            for name in FIELD_ONTOLOGY_MAP
            if name in by_name and by_name[name].type == "enum"
        )
        if enum_aligned:
            raise ValueError(
                "Ontology-aligned columns must not be declared 'enum' — vocabulary "
                "validation would overwrite the text a re-alignment reads back: "
                f"{enum_aligned}"
            )

    def align_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach alignment rows for the record's aligned fields.

        Canonical values are never modified. The rows travel on the record as
        ``_ontology_alignments`` so ``CanonicalStore.upsert`` writes them inside
        the same transaction as the canonical row; they carry no identity
        because the store supplies it from the primary key — and must not,
        since the enrichment path assigns ``Source``/``SourceID`` only after
        finalization.

        The payload is assigned, never extended: a completed pass is the
        record's whole alignment set, matching the store's set-replacement
        semantics. A carried-over row would collide with a fresh one on
        ``(field_name, ontology, part_index)`` and, since the store inserts
        without ``OR REPLACE``, roll back the canonical row along with it.

        Returns:
            A copy of the record. A completed pass carries
            ``_ontology_alignments`` and ``ontology_fingerprint``; an
            unreachable ontology carries neither and is marked pending instead.
        """
        result = dict(record)
        alignment_rows, unreachable = self.align_values(record)

        # An unreachable ontology means "not known yet", not "no longer
        # aligns": a short list here would delete rows a previous pass
        # resolved, and stamping the fingerprint would claim work that never
        # happened. The pending marker keeps the canonical row uncommitted, so
        # the sample is retried instead of being skipped by its content hash
        # forever. It overwrites the LLM-budget marker the pipeline may have
        # set; both mean pending, and this one is the stricter outcome.
        if unreachable:
            result["_status"] = {"reason": PENDING_ONTOLOGY_UNAVAILABLE}
            return result

        result["_ontology_alignments"] = alignment_rows
        result["ontology_fingerprint"] = self._fingerprint
        return result

    def align_values(
        self, values: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Align whichever aligned fields ``values`` holds.

        Takes a plain mapping rather than a record so the re-alignment CLI can
        pass a canonical row straight from the database.

        Returns:
            ``(alignment_rows, unreachable)``. A pass that found nothing to
            align returns an empty list and still owns the set: any rows a
            previous pass wrote describe text this record no longer holds.
        """
        parts = self._alignment_parts(values)
        if not parts:
            return [], False

        # A second connection to the store's file, not a store handle: the
        # cache outlives any one canonical row and is read on the extraction
        # path, where the store's connection is mid-transaction. The
        # ``ontology_cache`` table is created with the other sidecars, which is
        # why the caller is required to point this at the canonical database.
        conn = sqlite3.connect(str(self.cache_db))
        try:
            return self._align_parts(parts, conn)
        finally:
            conn.close()

    def _alignment_parts(
        self, record: dict[str, Any]
    ) -> list[tuple[str, str, str | None, int, str]]:
        """Enumerate the ``(field, ontology, version, part_index, text)`` to look up."""
        parts = []
        for field_name, ontology in FIELD_ONTOLOGY_MAP.items():
            value = record.get(field_name)
            if not value or not isinstance(value, str) or value.casefold() in {"null", "unknown"}:
                continue
            column = next(
                (col for col in self.schema.columns if col.name == field_name), None
            )
            version = self.ontology_versions.get(ontology)
            for part_index, matched_on in split_multi_value(
                value, column.delimiter if column else None
            ):
                parts.append((field_name, ontology, version, part_index, matched_on))
        return parts

    def _align_parts(
        self,
        parts: list[tuple[str, str, str | None, int, str]],
        conn: sqlite3.Connection,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Look up every part; flag whether any lookup could not be resolved."""
        alignment_rows = []
        unreachable = False
        for field_name, ontology, version, part_index, matched_on in parts:
            alignment_row = _align_single_value(
                field_name=field_name,
                matched_on=matched_on,
                part_index=part_index,
                ontology=ontology,
                ontology_version=version,
                cache=conn,
                http_client=self.http_client,
            )
            if alignment_row is None:
                unreachable = True
            else:
                alignment_rows.append(alignment_row)
        return alignment_rows, unreachable


def normalize_text(text: str) -> str:
    """Unicode NFKC → strip → collapse whitespace → casefold."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.strip()
    normalized = " ".join(normalized.split())
    return normalized.casefold()


def split_multi_value(value: str, delimiter: str | None = ";") -> list[tuple[int, str]]:
    """Split value by delimiter, returning (part_index, trimmed_text) for non-empty parts.

    A column that declares no delimiter is single-valued, so the whole string is
    one part — splitting it anyway would shred a term that merely contains the
    default separator, and ``str.split(None)`` would split it on whitespace.
    """
    if delimiter is None:
        trimmed = value.strip()
        return [(0, trimmed)] if trimmed else []
    if delimiter == ",":
        from metadata_agent.extract.review import _parts
        parts = _parts(value)
    else:
        parts = value.split(delimiter)
    result = []
    for idx, part in enumerate(parts):
        trimmed = part.strip()
        if trimmed:
            result.append((idx, trimmed))
    return result


def _align_single_value(
    field_name: str,
    matched_on: str,
    part_index: int,
    ontology: str,
    ontology_version: str | None,
    cache: Any,
    http_client: Any,
) -> dict[str, Any] | None:
    """Align a single value to an ontology term.

    Returns ``None`` when the ontology could not be reached, so the caller can
    tell "no term for this text" from "not looked up yet".
    """
    normalized_query = normalize_text(matched_on)

    candidates = _get_cached_candidates(cache, ontology, normalized_query, ontology_version)
    if candidates is None:
        candidates = _fetch_ols4_candidates(http_client, ontology, normalized_query)
        if candidates is None:
            return None
        _cache_candidates(cache, ontology, normalized_query, ontology_version, candidates)

    match_result = _match_candidates(matched_on, normalized_query, candidates)

    # Identity is supplied by the sidecar writer.
    return {
        "field_name": field_name,
        "ontology": ontology,
        "part_index": part_index,
        "matched_on": matched_on,
        "term_id": match_result.get("term_id"),
        "term_label": match_result.get("term_label"),
        "match_score": match_result.get("match_score"),
        "match_method": match_result.get("match_method"),
        "status": match_result["status"],
        "ontology_version": ontology_version,
        "matcher_version": MATCHER_VERSION,
        "candidates_json": match_result.get("candidates_json"),
    }


def _get_cached_candidates(
    cache: Any, ontology: str, normalized_query: str, ontology_version: str
) -> list[dict[str, Any]] | None:
    """Retrieve candidates from cache, return None if not found."""
    cursor = cache.execute(
        """
        SELECT response_json FROM ontology_cache
        WHERE ontology = ? AND normalized_query = ? AND ontology_version = ?
        """,
        (ontology, normalized_query, ontology_version),
    )
    row = cursor.fetchone()
    if row:
        return json.loads(row[0])
    return None


def _cache_candidates(
    cache: Any,
    ontology: str,
    normalized_query: str,
    ontology_version: str,
    candidates: list[dict[str, Any]],
) -> None:
    """Store candidates in cache using INSERT OR IGNORE for concurrent safety."""
    cache.execute(
        """
        INSERT OR IGNORE INTO ontology_cache (ontology, normalized_query, ontology_version, response_json)
        VALUES (?, ?, ?, ?)
        """,
        (ontology, normalized_query, ontology_version, json.dumps(candidates)),
    )
    cache.commit()


def _fetch_ols4_candidates(
    http_client: Any, ontology: str, normalized_query: str
) -> list[dict[str, Any]] | None:
    """Fetch candidates from OLS4, or ``None`` when the ontology did not answer.

    ``HttpClient`` has already retried 429/5xx/network failures five times, so
    a result that is still not ok is exhausted rather than transient. A non-429
    4xx and a malformed body are the same outcome as a network failure — the
    ontology could not be read — and none of them are cached, so the next pass
    retries rather than inheriting a hole.

    The *normalized* query is sent, not the original spelling: the cache is
    keyed by the normalized form, so querying with whichever casing happened to
    arrive first would make the request depend on cache history.
    """
    params = {
        "q": normalized_query,
        "ontology": ontology.lower(),
        "exact": "false",
        "obsoletes": "false",
        "rows": 100,
        "start": 0,
    }

    try:
        result = http_client.get(OLS4_SEARCH_URL, params=params)
    except AssertionError:
        # A test stub reporting an unexpected call, or a failed invariant —
        # never an unreachable ontology. Swallowing it would turn a coding
        # error into a permanent pending row.
        raise
    except Exception as exc:
        # Any other client failure is indistinguishable from the network being
        # down, and is handled the same way: defer, do not cache.
        logger.warning("OLS4 request raised for %s/%r: %s", ontology, normalized_query, exc)
        return None

    if result is None or not result.ok or not result.text:
        logger.warning(
            "OLS4 lookup failed for %s/%r: status=%r error=%r",
            ontology, normalized_query,
            getattr(result, "status", None), getattr(result, "error", None),
        )
        return None

    try:
        docs = json.loads(result.text)["response"]["docs"]
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning(
            "OLS4 returned an unreadable body for %s/%r: %s",
            ontology, normalized_query, exc,
        )
        return None
    if not isinstance(docs, list):
        logger.warning("OLS4 docs for %s/%r is not a list", ontology, normalized_query)
        return None

    candidates = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        term_id = doc.get("obo_id") or doc.get("iri")
        label = doc.get("label") or ""
        # Types are checked, not assumed: a non-string label reaches
        # normalize_text() and difflib, where it would raise outside the guard
        # above and abort the extraction instead of deferring it.
        if not isinstance(term_id, str) or not term_id or not isinstance(label, str):
            continue
        synonyms = doc.get("synonym") or []
        if isinstance(synonyms, str):
            synonyms = [synonyms]
        if not isinstance(synonyms, list):
            synonyms = []
        candidates.append({
            "term_id": term_id,
            "label": label,
            "synonyms": [s for s in synonyms if isinstance(s, str)],
        })
    return candidates


def _match_candidates(
    matched_on: str, normalized_query: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Resolve the query against the candidates by precedence, then by score.

    The four methods are distinct stages in the spec's order: an exact
    preferred label, a synonym that matches verbatim, a synonym that matches
    only once normalized, then a scored approximation. Which stage won is
    recorded, so an approximate hit is never filed under an exact one.
    """
    if not candidates:
        return {"status": "no_match"}

    exact_stages = (
        ("exact", lambda c: normalize_text(c["label"]) == normalized_query),
        ("synonym", lambda c: matched_on in c["synonyms"]),
        (
            "normalized_synonym",
            lambda c: any(normalize_text(s) == normalized_query for s in c["synonyms"]),
        ),
    )
    for method, hit in exact_stages:
        hits = [c for c in candidates if hit(c)]
        if hits:
            return _resolve(
                [{"candidate": c, "score": 1.0, "method": method} for c in hits]
            )

    scored = []
    for candidate in candidates:
        best = max(
            difflib.SequenceMatcher(
                None, normalized_query, normalize_text(text), autojunk=False
            ).ratio()
            for text in (candidate["label"], *candidate["synonyms"])
        )
        if best >= FUZZY_THRESHOLD:
            scored.append({"candidate": candidate, "score": best, "method": "fuzzy"})

    if not scored:
        return {"status": "no_match"}
    return _resolve(scored)


def _resolve(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Name the winner, or report the tie rather than guessing at it."""
    ordered = sorted(
        scored,
        key=lambda s: (
            -s["score"],
            s["candidate"]["label"].casefold(),
            s["candidate"]["term_id"],
        ),
    )
    best = ordered[0]
    tied = [s for s in ordered if best["score"] - s["score"] <= AMBIGUITY_TOLERANCE]
    if len(tied) > 1:
        return {"status": "ambiguous", "candidates_json": _candidates_json(tied)}
    return {
        "status": "matched",
        "term_id": best["candidate"]["term_id"],
        "term_label": best["candidate"]["label"],
        "match_score": best["score"],
        "match_method": best["method"],
    }


def _candidates_json(scored: list[dict[str, Any]]) -> str:
    """Each tied candidate with its own score and method, in resolved order."""
    return json.dumps([
        {
            "term_id": s["candidate"]["term_id"],
            "label": s["candidate"]["label"],
            "score": s["score"],
            "method": s["method"],
        }
        for s in scored
    ])
