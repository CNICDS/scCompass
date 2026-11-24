"""Per-field fill-rate report over the rows one run wrote.

The denominator is the set of identities whose canonical row the run committed,
not the table's row count: a run touching 50 samples in a 10,000-row database
reports its own completeness rather than the corpus average. Samples the run
deferred or found already current wrote no canonical row and so belong to
neither the denominator nor the fills; the caller reports those counts
separately rather than folding them in, because a deferral leaves whatever an
earlier run stored in place and crediting it here would report values this run
never produced.

Tier credit is granted per identity, and only where that identity's canonical
value is currently non-NULL and the schema routes the field through a tier. A
``field_acquisition`` row left behind by a value that later failed coercion
therefore cannot lift the tier counts above the fills. Every other non-NULL
value — the fields rules, the LLM and enrichment own, which is 34 of the 50 —
lands in ``untiered_count``. Those rules together are what keep
``non_null_count == sum(tier_counts) + untiered_count`` true for every field
rather than only for the tiered ones.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from metadata_agent.store.canonical import (
    ACQUISITION_TIERS,
    PRIMARY_KEY,
    CanonicalStore,
)

# Derived from the store's own vocabulary: a tier the store learns to persist
# must not be one this report silently drops into ``untiered_count``.
TIERS = tuple(sorted(ACQUISITION_TIERS))

# Each identity binds one value per primary-key column and contributes one row
# to the compound VALUES list, so this stays under both SQLite's 999-variable
# ceiling and the 500-row compound-select limit older builds enforce.
_BATCH_SIZE = min(400, 999 // len(PRIMARY_KEY))

_quote = CanonicalStore._quote_identifier
_IDENTITY_COLUMNS = ", ".join(_quote(name) for name in PRIMARY_KEY)


def _batched(identities: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for start in range(0, len(identities), _BATCH_SIZE):
        yield identities[start:start + _BATCH_SIZE]


def _identity_filter(count: int) -> str:
    row = f"({', '.join('?' for _ in PRIMARY_KEY)})"
    return f"({_IDENTITY_COLUMNS}) IN (VALUES {', '.join(row for _ in range(count))})"


def _distinct(run_identities: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    """The run's identities, deduplicated: a row updated twice is one row."""
    return list(dict.fromkeys(tuple(identity) for identity in run_identities))


def run_denominator(run_identities: Iterable[Sequence[Any]]) -> int:
    """The row count :func:`compute_fill_rate` reports against.

    Shared with the report so a caption cannot disagree with the percentages
    printed underneath it.
    """
    return len(_distinct(run_identities))


def _is_filled(value: Any) -> bool:
    """Whether a stored value answers the field.

    The rule is the store's own, from ``CanonicalStore._bind_value`` and
    ``export._cell``: anything that is not NULL and not the empty string is a
    value. ``unknown`` counts, because the store writes it wherever an enum
    column would otherwise be empty. Zero counts, because numeric columns
    arrive typed from INTEGER/REAL storage. Whitespace counts, because the
    store persisted it verbatim and the export sheet still shows it. The empty
    string reaches here only from a row written before ``_bind_value``
    normalised it away.
    """
    return value is not None and value != ""


def compute_fill_rate(
    run_identities: Iterable[Sequence[Any]],
    store: CanonicalStore,
) -> dict[str, dict[str, Any]]:
    """Report per-field completeness for the rows a run wrote.

    ``run_identities`` are the ``(Source, SourceID)`` pairs whose canonical row
    the run committed, repeats collapsing into one; ``store`` is read and left
    unmodified. Returns every manifest field in manifest order, mapped to
    ``{non_null_count, percent, tier_counts, untiered_count}``.
    """
    identities = _distinct(run_identities)
    denominator = len(identities)
    columns = store.columns
    tiered = store.tiered_columns
    report: dict[str, dict[str, Any]] = {
        name: {
            "non_null_count": 0,
            "percent": 0.0,
            "tier_counts": dict.fromkeys(TIERS, 0),
            "untiered_count": 0,
        }
        for name in columns
    }

    filled: set[tuple[Any, ...]] = set()
    canonical_select = ", ".join(_quote(name) for name in columns)
    for batch in _batched(identities):
        rows = store._conn.execute(
            f"SELECT {canonical_select} FROM {_quote('canonical')} "
            f"WHERE {_identity_filter(len(batch))}",
            [value for identity in batch for value in identity],
        )
        for row in rows:
            identity = tuple(row[name] for name in PRIMARY_KEY)
            for name in columns:
                if _is_filled(row[name]):
                    report[name]["non_null_count"] += 1
                    filled.add((*identity, name))

    acquisition_select = ", ".join(
        _quote(name) for name in (*PRIMARY_KEY, "field_name", "tier")
    )
    for batch in _batched(identities):
        rows = store._conn.execute(
            f"SELECT {acquisition_select} FROM {_quote('field_acquisition')} "
            f"WHERE {_identity_filter(len(batch))}",
            [value for identity in batch for value in identity],
        )
        for row in rows:
            field = row["field_name"]
            # A field the manifest no longer declares, one the schema does not
            # route through a tier, or a tier the store has since retired is
            # provenance the report cannot place; the value it describes is
            # still counted, as untiered.
            if field not in report or field not in tiered:
                continue
            if row["tier"] not in ACQUISITION_TIERS:
                continue
            identity = tuple(row[name] for name in PRIMARY_KEY)
            if (*identity, field) in filled:
                report[field]["tier_counts"][row["tier"]] += 1

    for metrics in report.values():
        metrics["untiered_count"] = (
            metrics["non_null_count"] - sum(metrics["tier_counts"].values())
        )
        if denominator:
            metrics["percent"] = 100.0 * metrics["non_null_count"] / denominator

    return report


def _rendered_tiers(metrics: dict[str, Any]) -> str:
    return " ".join(
        f"{tier}={count}" for tier, count in metrics["tier_counts"].items() if count
    )


def format_fill_rate(report: dict[str, dict[str, Any]], denominator: int) -> str:
    """Render the report as a table for a terminal run.

    Column widths come from the data because the manifest is profile-driven and
    the tier breakdown widens as tiers are implemented; a fixed width would
    misalign the moment either outgrew it.
    """
    field_width = max([len("Field"), *(len(name) for name in report)])
    tier_width = max([len("Tiers"), *(len(_rendered_tiers(m)) for m in report.values())])
    header = (
        f"{'Field':<{field_width}} {'Filled':>7} {'Pct':>7}  "
        f"{'Tiers':<{tier_width}} {'Untiered':>8}"
    )
    lines = [f"FILL RATE over {denominator} row(s)", header, "-" * len(header)]
    for field, metrics in report.items():
        lines.append(
            f"{field:<{field_width}} {metrics['non_null_count']:>7} "
            f"{metrics['percent']:>6.1f}%  {_rendered_tiers(metrics):<{tier_width}} "
            f"{metrics['untiered_count']:>8}"
        )
    return "\n".join(lines)
