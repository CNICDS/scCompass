from __future__ import annotations

import json
import logging
from contextlib import contextmanager, nullcontext
from threading import Event
from typing import Any

from metadata_agent.config.loader import ProfileSchema
from metadata_agent.enrich.bundle import content_hash as _bundle_content_hash
from metadata_agent.enrich.merge import is_fillable
from metadata_agent.extract.llm import LLMExtractor, LLMResponseError, needs_llm_fallback
from metadata_agent.extract.numeric import bounds_for, coerce_number
from metadata_agent.extract.review import ABSENT, grounded_answers, normalize_review_record
from metadata_agent.extract.rules import RulesExtractor
from metadata_agent.extract.source_evidence import reconcile_sample_evidence
from metadata_agent.extract.tiers import TierExtractor
from metadata_agent.extract.vocab_validator import validate_vocab
from metadata_agent.ontology.matcher import OntologyMatcher
from metadata_agent.runtime import KeyedLocks, count, timed
from metadata_agent.store.canonical import PENDING_LLM_REVIEW, defers_canonical_write
from metadata_agent.store.llm_cache import LLMCache

logger = logging.getLogger(__name__)

NUMERIC_COERCION_STAGE = "numeric_coercion"


def coerce_numeric_fields(
    record: dict[str, Any],
    schema: ProfileSchema | None,
    audit_log: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Coerce every numeric column the schema declares to its typed value.

    Absent keys stay absent: only values the extractors actually produced are
    converted, so a missing column is never turned into an explicit NULL.
    Rejected values become NULL and are audited; missing sentinels become NULL
    silently.
    """
    result = dict(record)
    if schema is None:
        return result

    for col in schema.columns:
        if not col.number or not col.numeric_kind or col.name not in result:
            continue
        original = result[col.name]
        if schema.review_policy and original == ABSENT:
            continue
        coerced, needs_audit = coerce_number(
            original, col.name, col.numeric_kind, bounds_for(col.name)
        )
        result[col.name] = coerced
        if needs_audit:
            logger.debug("Numeric rejection: %s=%r", col.name, original)
            if audit_log is not None:
                audit_log.append({
                    "column": col.name,
                    "rejected_value": str(original),
                    "stage": NUMERIC_COERCION_STAGE,
                })
    return result


def _content_hash(raw: dict[str, Any]) -> str:
    # Order-invariant bundle hash with no siblings; the enrichment path augments
    # it with sibling raws so a sibling change re-triggers extraction.
    return _bundle_content_hash(raw.get("raw", raw))


def _is_effective(value: Any) -> bool:
    """Whether a value answers the field rather than declining to.

    Numbers count as answers: the tool schema declares numeric fields as
    ``integer``/``number``, so the model returns ``42.5`` rather than ``"42.5"``
    and a str-only test would discard it. ``bool`` is not a metadata value.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped != "" and stripped.lower() != "unknown"
    return False


def _is_retainable(value: Any) -> bool:
    """Whether a declined value is still worth carrying into the record."""
    return value is not None and not isinstance(value, bool)


def _merge_rules_llm(
    rules_result: dict[str, str],
    llm_result: dict[str, Any],
    enum_columns: set[str],
    diagnostics: list[dict[str, str]],
    prefer_llm: set[str] | None = None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}

    all_keys = set(rules_result.keys()) | set(llm_result.keys())

    for key in all_keys:
        rules_val = rules_result.get(key)
        llm_val = llm_result.get(key)

        rules_effective = _is_effective(rules_val)
        llm_effective = _is_effective(llm_val)

        if rules_effective and llm_effective:
            if str(rules_val).strip().lower() != str(llm_val).strip().lower():
                diagnostics.append({
                    "column": key,
                    "type": "rules_llm_conflict",
                    "rules_value": str(rules_val),
                    "llm_value": str(llm_val),
                    "kept": "llm" if key in (prefer_llm or set()) else "rules",
                })
            merged[key] = llm_val if key in (prefer_llm or set()) else rules_val
        elif rules_effective:
            merged[key] = rules_val
        elif llm_effective:
            merged[key] = llm_val
        elif _is_retainable(rules_val):
            merged[key] = rules_val
        elif _is_retainable(llm_val):
            merged[key] = llm_val

    return merged


def _drop_unbacked_acquisitions(record: dict[str, Any]) -> None:
    """Discard provenance for fields that did not survive to a stored value.

    Coercion and vocabulary validation run after acquisition, so a value a tier
    produced can still end as NULL. Keeping its row would claim a tier filled a
    field it did not and would inflate the fill-rate denominator.
    """
    rows = record.get("_field_acquisition")
    if not isinstance(rows, list):
        return
    record["_field_acquisition"] = [
        row for row in rows
        if isinstance(row, dict) and not is_fillable(record.get(row.get("field_name")))
    ]


class ExtractionPipeline:
    def __init__(
        self,
        rules_extractor: RulesExtractor,
        llm_extractor: LLMExtractor | None = None,
        vocab_sets: dict[str, set[str]] | None = None,
        schema_json: str = "",
        vocab_summary: str = "",
        max_llm_calls: int = 100,
        profile_fingerprint: str = "",
        prompt_fingerprint: str = "",
        model_id: str = "",
        llm_cache: LLMCache | None = None,
        trigger_fields: list[str] | None = None,
        enum_columns: set[str] | None = None,
        schema: ProfileSchema | None = None,
        ontology_matcher: OntologyMatcher | None = None,
        tier_extractor: TierExtractor | None = None,
        vocab_aliases: dict[str, dict[str, str]] | None = None,
        legacy_resume_fingerprint: str | None = None,
        cancel_event: Event | None = None,
        request_locks: KeyedLocks | None = None,
    ):
        self._rules = rules_extractor
        self._llm = llm_extractor
        self._vocab_sets = vocab_sets or {}
        self._schema_json = schema_json
        self._vocab_summary = vocab_summary
        if max_llm_calls < 0:
            raise ValueError("Per-sample model call limit must be nonnegative")
        self._max_llm_calls = max_llm_calls
        self._cancel_event = cancel_event or Event()
        self._sample_active = False
        self._sample_calls_used = 0
        self._request_locks = request_locks
        self._llm_calls_used = 0
        self._last_llm_failure: str | None = None
        self._profile_fingerprint = profile_fingerprint
        self._legacy_resume_fingerprint = legacy_resume_fingerprint
        self._prompt_fingerprint = prompt_fingerprint
        self._model_id = model_id
        self._llm_cache = llm_cache
        self._trigger_fields = trigger_fields or []
        self._enum_columns = enum_columns or set()
        self._schema = schema
        self._ontology_matcher = ontology_matcher
        self._tier_extractor = tier_extractor
        self._vocab_aliases = vocab_aliases or {}

    @property
    def llm_calls_used(self) -> int:
        """Cumulative attempts for reporting, never the per-sample limiter."""
        return self._llm_calls_used

    @property
    def sample_llm_calls_used(self) -> int:
        return self._sample_calls_used

    @contextmanager
    def sample_scope(self):
        """One root sample and its siblings share one allowance.

        A pipeline belongs to one worker. Nested extraction calls retain the
        outer sample's allowance; the next root starts fresh, even on failure.
        """
        if self._sample_active:
            yield
            return
        self._sample_active = True
        self._sample_calls_used = 0
        self._last_llm_failure = None
        try:
            begin_sample = getattr(self._llm, "begin_sample", None)
            if begin_sample:
                begin_sample()
            yield
        finally:
            self._sample_active = False

    def close(self) -> None:
        close = getattr(self._llm, "close", None)
        if close:
            close()

    @property
    def profile_fingerprint(self) -> str:
        return self._profile_fingerprint

    @property
    def resume_fingerprints(self) -> tuple[str, ...]:
        """Old Messages-only runs can still resume under their original options."""
        legacy = self._legacy_resume_fingerprint
        return (self._profile_fingerprint, legacy) if legacy else (self._profile_fingerprint,)

    def should_skip(
        self,
        content_hash: str,
        stored_hash: str | None,
        stored_fingerprint: str | None,
    ) -> bool:
        if stored_hash is None:
            return False
        if content_hash != stored_hash:
            return False
        if stored_fingerprint != self._profile_fingerprint:
            return False
        return True

    def _extract_one_pre_validation(
        self,
        raw_record: dict[str, Any],
        stored_hash: str | None = None,
        stored_fingerprint: str | None = None,
        connector_id: str | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        with self.sample_scope():
            return self._extract_scoped(raw_record, stored_hash, stored_fingerprint, connector_id)

    def _extract_scoped(
        self,
        raw_record: dict[str, Any],
        stored_hash: str | None = None,
        stored_fingerprint: str | None = None,
        connector_id: str | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        """Extract fields from rules+LLM without numeric coercion or vocab validation.

        Returns (extracted_dict, content_hash) or None if skipped.
        """
        content_hash = _content_hash(raw_record)

        if self.should_skip(content_hash, stored_hash, stored_fingerprint):
            return None

        rules_result, diagnostics = self._rules.extract_diagnostics(
            raw_record, connector_id=connector_id
        )
        raw = raw_record.get("raw", raw_record)
        if connector_id == "gsa" and raw.get("_samc_parser") and raw.get("sample_url"):
            rules_result["Address"] = raw["sample_url"]

        llm_result: dict[str, Any] | None = None
        review = self._schema is not None and self._schema.review_policy
        if review:
            rules_result = normalize_review_record(rules_result, self._schema)
        if self._llm is not None and (review or (
            self._trigger_fields and needs_llm_fallback(rules_result, self._trigger_fields)
        )):
            try:
                # Cached answers remain usable after the network-call cap.
                llm_result = self._get_or_call_llm(content_hash, raw_record)
            except LLMResponseError as exc:
                # Both incomplete and invalid reviews remain retryable. A
                # committed row would stamp content_hash and skip the retry.
                logger.warning("deferring sample: %s", exc)
                rules_result["_status"] = {"reason": exc.pending_reason, "detail": str(exc)}
                return rules_result, content_hash

        if llm_result is not None:
            if review:
                llm_result = grounded_answers(llm_result, raw_record, self._schema, diagnostics)
            extracted = _merge_rules_llm(
                rules_result, llm_result, self._enum_columns, diagnostics,
                prefer_llm={c.name for c in self._schema.columns if c.llm_mode == "prefer"} if review else None,
            )
        else:
            extracted = rules_result

        if review:
            # Disabled fields belong to source-backed tiers/computation. Even
            # a cached or mocked model must not bypass this boundary.
            for col in self._schema.columns:
                if col.llm_mode == "disabled":
                    extracted.pop(col.name, None)
            extracted = normalize_review_record(extracted, self._schema)

        if review and self._llm is not None and llm_result is None:
            remedies = {
                "budget_exhausted": "sample allowance exhausted; resume or increase --max-llm-calls per sample",
                "cancelled": "run interrupted; resume this sample",
                "missing_api_key": "configure " + getattr(self._llm, "api_key_env_names",
                                                           "MODEL_API_KEY") + " and rerun",
                "request_failed": "check the preceding LLM extraction error and retry",
                "no_result": "the extractor returned no review result; retry",
            }
            cause = self._last_llm_failure or "no_result"
            extracted["_status"] = {
                "reason": PENDING_LLM_REVIEW,
                "detail": f"{cause}; sample calls={self.sample_llm_calls_used}/{self._max_llm_calls}; "
                          f"{remedies.get(cause, remedies['no_result'])}",
            }
        elif not review and self._llm is not None and self.sample_llm_calls_used >= self._max_llm_calls:
            if needs_llm_fallback(extracted, self._trigger_fields):
                extracted["_status"] = "pending"

        if diagnostics:
            extracted["_diagnostics"] = [*extracted.get("_diagnostics", []), *diagnostics]

        return extracted, content_hash

    def _finalize_one(
        self,
        extracted: dict[str, Any],
        content_hash: str,
        raw_record: dict[str, Any] | None = None,
        connector_id: str | None = None,
    ) -> dict[str, Any]:
        """Acquire tiered gaps, coerce numbers, align, then validate vocabularies.

        Tiers run here rather than beside rules and the LLM because this is the
        one step that runs exactly once per canonical record: the enrichment
        path calls ``_extract_one_pre_validation`` again for every sibling, and
        acquiring there would repeat each tier's network calls per sibling and
        fill fields a cheaper sibling was about to supply.

        Alignment runs on the pre-vocabulary text and after enrichment has
        merged in sibling values: validation replaces an out-of-vocab value with
        ``other``/``unknown``, so aligning afterwards would match against text
        the record no longer holds.
        """
        audit_log: list[dict[str, str]] = []

        if defers_canonical_write(extracted.get("_status")):
            # The store will write only the pending row, so tier acquisition and
            # ontology lookups here would spend their network calls on values
            # nothing reads. Only the keys the store requires are stamped.
            extracted["content_hash"] = content_hash
            extracted["profile_fingerprint"] = self._profile_fingerprint
            return extracted

        if self._tier_extractor is not None:
            with timed("tiers"):
                extracted = self._tier_extractor.acquire(
                    extracted, raw_record or {}, connector_id
                )
            if defers_canonical_write(extracted.get("_status")):
                extracted["content_hash"] = content_hash
                extracted["profile_fingerprint"] = self._profile_fingerprint
                return extracted

        if self._schema is not None and self._schema.review_policy:
            extracted = reconcile_sample_evidence(extracted, raw_record or {}, self._schema, connector_id)
            for diagnostic in extracted.get("_diagnostics", []):
                if "value" in diagnostic:
                    audit_log.append({
                        "column": diagnostic["column"],
                        "rejected_value": diagnostic["value"],
                        "stage": diagnostic["type"],
                    })
        extracted = coerce_numeric_fields(extracted, self._schema, audit_log)
        if "_field_evidence" in extracted:
            extracted["_field_evidence"] = [
                {**item, "normalized_value": str(extracted[item["field_name"]])
                 if extracted.get(item["field_name"]) is not None else ""}
                for item in extracted["_field_evidence"]
            ]

        if self._ontology_matcher is not None:
            with timed("ontology"):
                extracted = self._ontology_matcher.align_record(extracted)

        extracted = validate_vocab(
            extracted,
            self._vocab_sets,
            audit_log=audit_log,
            schema=self._schema,
            aliases=self._vocab_aliases,
        )
        _drop_unbacked_acquisitions(extracted)
        extracted["content_hash"] = content_hash
        extracted["profile_fingerprint"] = self._profile_fingerprint
        extracted["_audit_log"] = audit_log
        return extracted

    def extract_one(
        self,
        raw_record: dict[str, Any],
        stored_hash: str | None = None,
        stored_fingerprint: str | None = None,
        connector_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Extract and validate a single record (legacy single-step method)."""
        with self.sample_scope():
            with timed("primary"):
                result = self._extract_one_pre_validation(
                    raw_record, stored_hash, stored_fingerprint, connector_id
                )
            if result is None:
                return None
            extracted, content_hash = result
            return self._finalize_one(extracted, content_hash, raw_record, connector_id)

    def _get_or_call_llm(self, content_hash: str, raw_record: dict[str, Any]) -> dict[str, Any] | None:
        key = (content_hash, self._prompt_fingerprint, self._model_id)
        # The cache is checked after acquiring the per-key lock so concurrent
        # samples sharing a sibling spend at most one model call for that raw.
        with self._request_locks.hold(key) if self._request_locks else nullcontext():
            return self._get_or_call_llm_locked(content_hash, raw_record)

    def _get_or_call_llm_locked(self, content_hash: str, raw_record: dict[str, Any]) -> dict[str, Any] | None:
        self._last_llm_failure = None
        if self._llm_cache:
            cached = self._llm_cache.get(
                content_hash, self._prompt_fingerprint, self._model_id
            )
            if cached is not None:
                count("llm_cache_hits")
                return cached

        if self._cancel_event.is_set():
            self._last_llm_failure = "cancelled"
            return None
        if self._sample_calls_used >= self._max_llm_calls:
            self._last_llm_failure = "budget_exhausted"
            return None

        calls_before = getattr(self._llm, "calls_made", None)
        entered = False
        try:
            raw_text = json.dumps(raw_record.get("raw", raw_record), ensure_ascii=False, indent=2)
            entered = True
            with timed("llm"):
                llm_result = self._llm.extract(raw_text, self._schema_json, self._vocab_summary)
        finally:
            calls_after = getattr(self._llm, "calls_made", None)
            if isinstance(calls_before, int) and isinstance(calls_after, int):
                attempts = max(0, calls_after - calls_before)
            else:
                # Extractor adapters without a counter get one attempt per call.
                attempts = int(entered)
            self._llm_calls_used += attempts
            self._sample_calls_used += attempts
            count("llm_calls", attempts)
        if llm_result is not None:
            if self._llm_cache:
                self._llm_cache.put(
                    content_hash, self._prompt_fingerprint, self._model_id, llm_result
                )
        else:
            self._last_llm_failure = getattr(self._llm, "last_failure_reason", None) or "no_result"
        return llm_result
