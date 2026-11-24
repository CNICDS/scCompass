from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from openpyxl import Workbook

from metadata_agent.store.canonical import PRIMARY_KEY, CanonicalStore

# Excel parses a cell beginning with any of these as a formula. Values are
# written verbatim otherwise, so a leading apostrophe is added to keep extracted
# text as text. The schema contract names U+2212 MINUS SIGN; ASCII "-", tab and
# CR are escaped alongside it because those are what a spreadsheet actually
# treats as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "−", "@", "\t", "\r")

# Public so a reader binds to the sheet it means rather than to sheet 0, which
# is only the metadata sheet for as long as nothing is written ahead of it.
METADATA_SHEET = "metadata"
ONTOLOGY_SHEET = "ontology"

# The published alignment columns. `candidates_json` is stored but withheld: it
# is diagnostic detail behind an ambiguous match, not part of the contract.
# `field_name` carries internal keys, not `export_as` aliases, so an alias on an
# aligned field would leave the two sheets unjoinable; none of the aligned
# fields declares one.
_ONTOLOGY_COLUMNS = (
    *PRIMARY_KEY,
    "field_name",
    "part_index",
    "ontology",
    "matched_on",
    "term_id",
    "term_label",
    "match_score",
    "match_method",
    "status",
    "ontology_version",
    "matcher_version",
    "aligned_at",
)

_quote = CanonicalStore._quote_identifier
_SOURCE_COLUMN, _ = PRIMARY_KEY
_IDENTITY_ORDER = ", ".join(_quote(name) for name in PRIMARY_KEY)


def _cell(value: Any) -> Any:
    """Prepare one canonical value for its xlsx cell.

    Numeric columns arrive as ``int``/``float`` from their INTEGER/REAL storage
    and are written unchanged, so they stay sortable numbers rather than text.
    Escaping keys off the value's type rather than the column's: a legacy string
    parked in a numeric column still must not become a formula.
    """
    if value is None or value == "" or value == "unknown":
        return None
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _field_ordinal(manifest_columns: Sequence[str]) -> tuple[str, list[str]]:
    """SQL ranking an alignment row by its field's position in the manifest.

    Ordering on position rather than field name is what makes the ontology sheet
    read in the metadata sheet's column order. A field the manifest no longer
    declares — an alignment left behind by a schema change — sorts after every
    declared one, sharing that rank with the other undeclared fields, so the
    caller must add a name tie-break for the order to be reproducible at all.

    Returns the expression with its bound field names, because the two are one
    contract: the manifest keys and the aliases they export under are both
    fifty strings, and binding the wrong list would sort silently, not fail.
    """
    branches = " ".join(f"WHEN ? THEN {index}" for index in range(len(manifest_columns)))
    return (
        f'CASE {_quote("field_name")} {branches} ELSE {len(manifest_columns)} END',
        list(manifest_columns),
    )


def _select(
    table: str,
    columns: Sequence[str],
    order: tuple[str, Sequence[Any]],
    source_filter: str | None,
) -> tuple[str, list[Any]]:
    """Build one sheet's SELECT, filtered on the source and ordered.

    Identifiers are quoted because column names come from schema.yaml, which
    CanonicalStore parses without running validate_profile_schema, so an
    embedded quote would otherwise break out of the identifier and into the
    SELECT. Filtering on the leading identity column follows canonical.py's
    positional treatment of ``PRIMARY_KEY``.
    """
    order_by, order_params = order
    selected = ", ".join(_quote(name) for name in columns)
    query = f"SELECT {selected} FROM {_quote(table)}"
    params: list[Any] = []
    if source_filter:
        query += f" WHERE {_quote(_SOURCE_COLUMN)} = ?"
        params.append(source_filter)
    # WHERE placeholders precede ORDER BY ones in the statement, so the filter
    # has to bind ahead of the ordering.
    return f"{query} ORDER BY {order_by}", [*params, *order_params]


def _write_sheet(
    workbook: Workbook,
    title: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> None:
    sheet = workbook.create_sheet(title=title)
    # Headers are escaped too: a column named "=1+1" would otherwise be written
    # as a live formula rather than as its own name.
    sheet.append([_cell(header) for header in headers])
    for row in rows:
        sheet.append([_cell(value) for value in row])


def export_xlsx(
    db_path: str | Path = "data/canonical.sqlite",
    output_path: str | Path = "output/metadata_export.xlsx",
    source_filter: str | None = None,
    *,
    profile_dir: Path,
    allow_destructive_rebuild: bool = False,
) -> Path:
    store = CanonicalStore(
        db_path=db_path,
        profile_dir=profile_dir,
        allow_destructive_rebuild=allow_destructive_rebuild,
    )
    try:
        manifest_columns = store.columns
        export_headers = store.export_headers
        metadata_query, metadata_params = _select(
            "canonical", manifest_columns, (_IDENTITY_ORDER, ()), source_filter
        )
        field_ordinal, ordinal_params = _field_ordinal(manifest_columns)
        ontology_order = ", ".join((
            _IDENTITY_ORDER,
            field_ordinal,
            _quote("part_index"),
            _quote("ontology"),
            _quote("field_name"),
        ))
        ontology_query, ontology_params = _select(
            "ontology_alignment",
            _ONTOLOGY_COLUMNS,
            (ontology_order, ordinal_params),
            source_filter,
        )
        metadata_rows = store._conn.execute(metadata_query, metadata_params).fetchall()
        absence = {
            (row[0], row[1], row[2])
            for row in store._conn.execute(
                f'SELECT {", ".join(_quote(n) for n in PRIMARY_KEY)}, field_name FROM field_absence'
            )
        }
        identity_indices = [manifest_columns.index(name) for name in PRIMARY_KEY]
        metadata_rows = [
            ["null" if (*(row[i] for i in identity_indices), field) in absence else row[j]
             for j, field in enumerate(manifest_columns)]
            for row in metadata_rows
        ]
        ontology_rows = store._conn.execute(ontology_query, ontology_params).fetchall()
    finally:
        store.close()

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook(write_only=True)
    try:
        _write_sheet(workbook, METADATA_SHEET, export_headers, metadata_rows)
        _write_sheet(workbook, ONTOLOGY_SHEET, _ONTOLOGY_COLUMNS, ontology_rows)
        workbook.save(str(out))
    finally:
        workbook.close()
    return out
