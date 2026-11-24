from __future__ import annotations

from typing import Any

_GENDER_ALIASES: dict[str, str] = {
    "female": "female",
    "f": "female",
    "male": "male",
    "m": "male",
    "unknown": "unknown",
}

_GENDER_FEMALE_TOKENS = {"female", "f"}
_GENDER_MALE_TOKENS = {"male", "m"}


def _normalize_gender(raw: str) -> str:
    """Gender normalization kept for backward compatibility, but vocab validator
    now handles all case folding via alias matching."""
    folded = raw.strip().lower()
    if not folded:
        return "unknown"
    if folded in _GENDER_ALIASES:
        return _GENDER_ALIASES[folded]
    parts = [p.strip() for p in folded.replace("/", " ").replace(",", " ").split()]
    has_female = any(p in _GENDER_FEMALE_TOKENS for p in parts)
    has_male = any(p in _GENDER_MALE_TOKENS for p in parts)
    if has_female and has_male:
        return "unknown"
    if has_female:
        return "female"
    if has_male:
        return "male"
    return "unknown"


def _normalize_default(raw: str) -> str:
    """Whitespace-only normalization. Case folding is now handled by the vocab
    validator's alias resolution, so canonical values remain case-exact."""
    stripped = raw.strip()
    return stripped if stripped else "unknown"


_COLUMN_NORMALIZERS: dict[str, Any] = {
    "Gender": _normalize_gender,
}


def normalize_enum_value(column: str, raw: str | None) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "unknown"
    raw_str = str(raw)
    fn = _COLUMN_NORMALIZERS.get(column, _normalize_default)
    return fn(raw_str)


def normalize_record(
    record: dict[str, Any],
    columns: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    out = dict(record)
    for col in columns:
        val = out.get(col)
        if val is not None and val != "":
            out[col] = normalize_enum_value(col, val)
    return out
