from __future__ import annotations

import logging
from typing import Any

from metadata_agent.config.loader import ProfileSchema

logger = logging.getLogger(__name__)

MISSING = "unknown"
OTHER = "other"

# Only REJECTED_ENUM_STAGE discards a value; the other two record values that
# survive validation without matching the vocabulary.
REJECTED_ENUM_STAGE = "vocab_validation"
BUCKETED_OTHER_STAGE = "vocab_bucketed_other"
PRESERVED_SEMI_STAGE = "vocab_preserved_semi"


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _audit(
    audit_log: list[dict[str, str]] | None,
    field: str,
    value: Any,
    stage: str,
) -> None:
    if audit_log is not None:
        audit_log.append({
            "column": field,
            "rejected_value": str(value),
            "stage": stage,
        })


def _member(allowed: set[str], target: str) -> str | None:
    """The vocabulary member equal to ``target`` ignoring case, if any.

    Canonical values carry their contract casing (``Other``, ``unknown``), so a
    literal lookup would miss the very fallback the column declares.
    """
    if target in allowed:
        return target
    folded = target.casefold()
    return next((value for value in sorted(allowed) if value.casefold() == folded), None)


def _resolve(value: Any, allowed: set[str], aliases: dict[str, str] | None) -> str | None:
    """Map an extracted value onto its canonical vocabulary form, or ``None``.

    Matching is case-insensitive and the canonical spelling always wins, so a
    model answering ``"human"`` or ``"HUMAN"`` still stores ``Human``. Aliases are
    consulted last: they rename a value, and a value that already names itself
    needs no renaming.
    """
    text = str(value).strip()
    if text in allowed:
        return text
    exact = _member(allowed, text)
    if exact is not None:
        return exact
    if aliases:
        canonical = aliases.get(text.casefold())
        if canonical is not None:
            return _member(allowed, canonical) or canonical
    return None


def _partition_fields(
    enums: dict[str, set[str]], schema: ProfileSchema | None
) -> tuple[list[str], list[str]]:
    """Split the schema's validated columns into (enum, semi-controlled).

    Without a schema every vocabulary key is treated as an enum column. Callers
    derive those keys from a schema either way, so no field name is hardcoded.
    """
    if schema is None:
        return list(enums), []
    enum_fields = [c.name for c in schema.columns if c.type == "enum" and not c.system]
    semi_fields = [c.name for c in schema.columns if c.type == "semi" and not c.system]
    return enum_fields, semi_fields


def _validate_enum(
    result: dict[str, Any],
    field: str,
    allowed: set[str] | None,
    aliases: dict[str, str] | None,
    audit_log: list[dict[str, str]] | None,
) -> None:
    val = result.get(field)
    if _is_empty(val):
        result[field] = MISSING
        return

    if allowed is None:
        result[field] = str(val).strip()
        return

    resolved = _resolve(val, allowed, aliases)
    if resolved is not None:
        result[field] = resolved
        return

    # A vocabulary offering "other" buckets recognized-but-unlisted values
    # there; "unknown" stays reserved for missing data.
    other = _member(allowed, OTHER)
    bucket = other or _member(allowed, MISSING) or MISSING
    stage = BUCKETED_OTHER_STAGE if other else REJECTED_ENUM_STAGE
    logger.debug("Out-of-vocab: %s=%r → %s", field, val, bucket)
    _audit(audit_log, field, val, stage)
    result[field] = bucket


def _validate_semi(
    result: dict[str, Any],
    field: str,
    allowed: set[str] | None,
    aliases: dict[str, str] | None,
    audit_log: list[dict[str, str]] | None,
) -> None:
    """Keep the original text: the ontology aligner reads this column later."""
    val = result.get(field)
    if _is_empty(val) or allowed is None:
        return
    if _resolve(val, allowed, aliases) is None:
        _audit(audit_log, field, val, PRESERVED_SEMI_STAGE)


def validate_vocab(
    extracted: dict[str, Any],
    enums: dict[str, set[str]],
    audit_log: list[dict[str, str]] | None = None,
    schema: ProfileSchema | None = None,
    aliases: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate the controlled-vocabulary columns the active schema declares.

    Never writes a key the schema does not declare: injecting a stale field name
    makes ``CanonicalStore.upsert`` reject the entire record.
    """
    result = dict(extracted)
    enum_fields, semi_fields = _partition_fields(enums, schema)
    alias_map = aliases or {}

    for field in enum_fields:
        if schema is not None and schema.review_policy:
            value = result.get(field)
            if _is_empty(value) or str(value).casefold() == "unknown":
                result[field] = ""
                continue
            if value == "null":
                continue
            allowed = enums.get(field)
            if allowed is not None and _resolve(value, allowed, alias_map.get(field)) is None:
                _audit(audit_log, field, value, REJECTED_ENUM_STAGE)
                result[field] = ""
                continue
        _validate_enum(result, field, enums.get(field), alias_map.get(field), audit_log)
        if schema is not None and schema.review_policy and result.get(field) == MISSING:
            result[field] = ""
    for field in semi_fields:
        if result.get(field) == "null":
            continue
        _validate_semi(result, field, enums.get(field), alias_map.get(field), audit_log)

    return result
