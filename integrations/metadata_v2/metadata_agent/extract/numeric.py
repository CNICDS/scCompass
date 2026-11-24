"""Typed numeric coercion for schema columns declaring ``number: true``.

Commas are read only as thousands separators: the leading group carries one to
three digits and every later group exactly three. ``"1,500"`` parses; ``"1,5"``
is rejected rather than guessed to be either 1.5 or 15.

Coercion is total. Every value either yields a number, a silent NULL (missing
sentinel), or an audited NULL — it never raises, because a single malformed
cell must not abort extraction of the record that holds it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias

from metadata_agent.store.db import SQLITE_INT_MAX, SQLITE_INT_MIN

NumericKind: TypeAlias = Literal["integer", "real"]
Bound: TypeAlias = int | float | None
Bounds: TypeAlias = tuple[Bound, Bound]

UNBOUNDED: Bounds = (None, None)

# Absent data, not bad data: these produce NULL without an audit row.
MISSING_SENTINELS = frozenset({
    "unknown", "n/a", "na", "null", "none", "missing",
    "not available", "not collected", "not provided",
})

# Fields whose source values legitimately carry a trailing percent sign. The
# sign is dropped without rescaling: "12.5%" means 12.5 on a 0-100 scale.
PERCENT_SUFFIX_FIELDS = frozenset({"DoubletsPerc"})

_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_GROUPED_NUMBER = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d*)?(?:[eE][+-]?\d+)?")

_STATIC_BOUNDS: dict[str, Bounds] = {
    "Age": (0, 150),
    "RawSize_GB": (0, None),
    "CellNumberRaw": (0, None),
    "CellNumberAfterQC": (0, None),
    "CellNumberAfterAnnotation": (0, None),
    "MedianUmi": (0, None),
    "DoubletsPerc": (0, 100),
    "Pubmed ID": (1, 9223372036854775807),
}


def bounds_for(field_name: str) -> Bounds:
    """Inclusive bounds for a field; ``UNBOUNDED`` when none are declared.

    ``Year`` is resolved per call: freezing ``current_year + 1`` at import time
    would start rejecting valid years in a long-lived process.
    """
    if field_name == "Year":
        return (1000, datetime.now(timezone.utc).year + 1)
    return _STATIC_BOUNDS.get(field_name, UNBOUNDED)


def coerce_number(
    value: object,
    field_name: str,
    numeric_kind: NumericKind,
    bounds: Bounds,
) -> tuple[int | float | None, bool]:
    """Coerce ``value`` to ``numeric_kind``.

    Returns ``(number, False)`` when coerced, ``(None, False)`` for a missing
    sentinel, and ``(None, True)`` for anything invalid or out of bounds — the
    second element is the caller's signal to write an audit row.
    """
    text: str | None = None
    if value is None:
        return None, False
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() in MISSING_SENTINELS:
            return None, False

    # bool is an int subclass, so it must be refused before numeric handling.
    if isinstance(value, bool):
        return None, True

    number = (
        _parse_text(text, allow_percent=field_name in PERCENT_SUFFIX_FIELDS)
        if text is not None
        else _from_number(value)
    )
    if number is None:
        return None, True

    if numeric_kind == "integer":
        integral = number.to_integral_value()
        if number != integral:
            return None, True
        number = integral
        if not SQLITE_INT_MIN <= number <= SQLITE_INT_MAX:
            return None, True

    if not _within_bounds(number, bounds):
        return None, True

    return _finalize(number, numeric_kind)


def _within_bounds(number: Decimal, bounds: Bounds) -> bool:
    """Compare against inclusive bounds, treating an unusable bound as a miss.

    A NaN or infinite bound is a caller error, but raising here would break the
    totality this module guarantees, so the value is rejected and audited.
    """
    for bound, too_far in ((bounds[0], Decimal.__lt__), (bounds[1], Decimal.__gt__)):
        if bound is None:
            continue
        if isinstance(bound, float) and not math.isfinite(bound):
            return False
        if too_far(number, Decimal(str(bound))):
            return False
    return True


def _finalize(number: Decimal, numeric_kind: NumericKind) -> tuple[int | float | None, bool]:
    if numeric_kind == "integer":
        return int(number), False
    real = float(number)
    # float() saturates to inf for magnitudes beyond the double range instead
    # of raising, so an unbounded field could otherwise store inf.
    return (real, False) if math.isfinite(real) else (None, True)


def _parse_text(text: str, *, allow_percent: bool) -> Decimal | None:
    if text.endswith("%"):
        if not allow_percent:
            return None
        text = text[:-1].strip()
    if not text:
        return None

    if "," in text:
        if not _GROUPED_NUMBER.fullmatch(text):
            return None
        text = text.replace(",", "")
    elif not _PLAIN_NUMBER.fullmatch(text):
        return None

    try:
        return Decimal(text)
    except InvalidOperation:
        # Exponents too large for the Decimal context, e.g. "1e999999999999999".
        return None


def _from_number(value: object) -> Decimal | None:
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    return None
