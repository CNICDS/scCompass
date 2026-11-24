"""Non-destructive field-level enrichment merge.

Sibling attributes fill a sample's canonical row only where the primary value is
*fillable* (empty or a placeholder); an effective primary value is never
overwritten, and identity columns (``Source``/``SourceID``) are immutable. When
several siblings can fill the same empty field, precedence is deterministic
(primary → BioSample hub → INSDC sample → run/experiment mirror, ties by
ascending accession) and every discarded conflict is recorded in
``_diagnostics``.
"""
from __future__ import annotations

from typing import Any

# Case-insensitive placeholders treated as "no effective value".
FILLABLE_PLACEHOLDERS = {
    "unknown", "not applicable", "n/a", "na", "none",
    "missing", "not collected", "not provided",
}

_IMMUTABLE = {"Source", "SourceID"}
_META = {
    "content_hash", "profile_fingerprint", "ontology_fingerprint",
    "_audit_log", "_diagnostics", "_status",
}


def is_fillable(value: Any) -> bool:
    """True when ``value`` is absent, blank, or a placeholder — i.e. safe to fill."""
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.lower() in FILLABLE_PLACEHOLDERS


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def sibling_priority(connector: str, relation: str) -> int:
    """Deterministic precedence rank (lower fills first)."""
    if connector == "biosample":
        return 0
    if relation == "sample":  # INSDC sample (SRS/ERS) or GEO GSM
        return 1
    return 2  # experiment / other mirror


def merge_enrichment(
    primary: dict[str, Any],
    siblings: list[tuple[Any, dict[str, Any]]],
    *,
    diagnostics: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Fill fillable primary columns from ``siblings`` (``(SiblingRef, canonical)``).

    Never mutates ``Source``/``Sample ID``, never overwrites an effective primary
    value; records every discarded conflicting sibling value in ``diagnostics``.
    """
    filled = dict(primary)
    diag = diagnostics if diagnostics is not None else []

    ordered = sorted(
        siblings,
        key=lambda item: (sibling_priority(item[0].connector, item[0].relation), item[0].accession),
    )

    for ref, canonical in ordered:
        for column, value in canonical.items():
            if column in _IMMUTABLE or column in _META or column.startswith("_"):
                continue
            if is_fillable(value):
                continue  # sibling has nothing effective to contribute
            current = filled.get(column)
            if is_fillable(current):
                filled[column] = value
            elif _norm(current) != _norm(value):
                diag.append({
                    "column": column,
                    "type": "enrichment_conflict",
                    "kept": str(current),
                    "discarded": str(value),
                    "source": f"{ref.connector}:{ref.accession}",
                })

    return filled
