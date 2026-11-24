from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from metadata_agent.config.loader import ColumnDef, ProfileSchema, parse_profile_schema
from metadata_agent.store.db import SQLITE_INT_MAX, SQLITE_INT_MIN, open_db

logger = logging.getLogger(__name__)

_META_COLUMNS = ("content_hash", "profile_fingerprint", "ontology_fingerprint")

# Metadata a producer must supply. ``ontology_fingerprint`` is excluded: it is
# written by ontology alignment, and a row extracted before alignment runs is
# legitimately missing it. Demanding it here would reject every plain upsert.
_REQUIRED_META_COLUMNS = ("content_hash", "profile_fingerprint")

# The canonical identity. Public because other modules must not re-declare it:
# a second copy is what let `Sample ID` survive a rename in one place only.
PRIMARY_KEY = ("Source", "SourceID")
_PRIMARY_KEY = PRIMARY_KEY
_REBUILD_FLAG = "--allow-canonical-rebuild"
_DEFAULT_PROFILE_DIR = Path("profiles/human_cellline")

# Underscore keys upsert() understands. Anything else is a producer mistake:
# the old code accepted any underscore key and silently discarded it.
# `_diagnostics` is the one key deliberately accepted without being persisted —
# callers carry it for logging, and there is no diagnostics sidecar.
_ACCEPTED_META_KEYS = frozenset({
    "_audit_log",
    "_ontology_alignments",
    "_field_acquisition",
    "_field_evidence",
    "_diagnostics",
    "_status",
})

_NOW = "(datetime('now'))"

# (name, sqlite_type, not_null, default). The default belongs in the signature:
# a table whose timestamp column lost its DEFAULT still matches on name, type
# and nullability, and would then reject every insert that omits that column.
_ColumnSignature = tuple[str, str, bool, str | None]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _normalized_default(default: str | None) -> str | None:
    """Compare defaults the way SQLite reports them.

    ``PRAGMA table_info`` echoes ``DEFAULT (datetime('now'))`` back as
    ``datetime('now')``, so the declared form is stripped to match.
    """
    if default is None:
        return None
    text = default.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text or None


@dataclass(frozen=True)
class _Column:
    name: str
    sqlite_type: str = "TEXT"
    not_null: bool = False
    default: str | None = None

    def ddl(self) -> str:
        parts = [_quote(self.name), self.sqlite_type]
        if self.not_null:
            parts.append("NOT NULL")
        if self.default:
            parts.append(f"DEFAULT {self.default}")
        return " ".join(parts)

    def signature(self) -> _ColumnSignature:
        return (
            self.name,
            self.sqlite_type.upper(),
            self.not_null,
            _normalized_default(self.default),
        )


@dataclass(frozen=True)
class _SidecarTable:
    """One sidecar table, declared once and used for both DDL and verification.

    A second copy of a column list is what lets DDL and expectations drift, so
    ``ddl()`` and ``layout()`` are generated from the same tuple.
    """

    name: str
    columns: tuple[_Column, ...]
    primary_key: tuple[str, ...]
    identity_keyed: bool

    def ddl(self) -> str:
        definitions = [column.ddl() for column in self.columns]
        definitions.append(
            "PRIMARY KEY (" + ", ".join(_quote(n) for n in self.primary_key) + ")"
        )
        return (
            f"CREATE TABLE {_quote(self.name)} (\n    "
            + ",\n    ".join(definitions)
            + "\n)"
        )

    def layout(self) -> tuple[tuple[_ColumnSignature, ...], tuple[str, ...]]:
        return tuple(c.signature() for c in self.columns), self.primary_key


def _identity_columns() -> tuple[_Column, ...]:
    return tuple(_Column(name, not_null=True) for name in _PRIMARY_KEY)


def _sidecar_tables() -> tuple[_SidecarTable, ...]:
    """Declare the sidecars, keyed to whatever identity canonical currently uses.

    Deriving the identity columns from ``_PRIMARY_KEY`` rather than naming them
    literally means renaming the identity column renames it here too, and
    ``_ensure_sidecar_tables()`` then migrates existing tables to match.
    """
    identity = _identity_columns()
    return (
        _SidecarTable(
            "extraction_audit",
            identity + (
                _Column("column", not_null=True),
                _Column("rejected_value", not_null=True),
                _Column("stage", not_null=True),
                _Column("run_ts", not_null=True, default=_NOW),
            ),
            (*_PRIMARY_KEY, "column", "rejected_value", "stage"),
            identity_keyed=True,
        ),
        _SidecarTable(
            "ontology_alignment",
            identity + (
                _Column("field_name", not_null=True),
                _Column("ontology", not_null=True),
                _Column("part_index", "INTEGER", not_null=True),
                _Column("matched_on", not_null=True),
                _Column("term_id"),
                _Column("term_label"),
                _Column("match_score", "REAL"),
                _Column("match_method"),
                _Column("status", not_null=True),
                _Column("ontology_version", not_null=True),
                _Column("matcher_version", not_null=True),
                _Column("aligned_at", not_null=True, default=_NOW),
                _Column("candidates_json"),
            ),
            (*_PRIMARY_KEY, "field_name", "ontology", "part_index"),
            identity_keyed=True,
        ),
        _SidecarTable(
            "field_absence",
            identity + (
                _Column("field_name", not_null=True),
                _Column("recorded_at", not_null=True, default=_NOW),
            ),
            (*_PRIMARY_KEY, "field_name"),
            identity_keyed=True,
        ),
        _SidecarTable(
            "field_acquisition",
            identity + (
                _Column("field_name", not_null=True),
                _Column("tier", not_null=True),
                _Column("source_ref"),
                _Column("acquired_at", not_null=True, default=_NOW),
            ),
            (*_PRIMARY_KEY, "field_name"),
            identity_keyed=True,
        ),
        _SidecarTable(
            "extraction_pending",
            identity + (
                _Column("content_hash"),
                _Column("reason", not_null=True),
                _Column("updated_at", not_null=True, default=_NOW),
            ),
            _PRIMARY_KEY,
            identity_keyed=True,
        ),
        _SidecarTable(
            "field_evidence",
            identity + (
                _Column("evidence_index", "INTEGER", not_null=True),
                *(_Column(name, not_null=True) for name in (
                    "field_name", "source_ref", "source_path", "source_text", "normalized_value", "method",
                )),
                _Column("recorded_at", not_null=True, default=_NOW),
            ),
            (*_PRIMARY_KEY, "evidence_index"),
            identity_keyed=True,
        ),
        _SidecarTable(
            "ontology_cache",
            (
                _Column("ontology", not_null=True),
                _Column("normalized_query", not_null=True),
                _Column("ontology_version", not_null=True),
                _Column("response_json"),
                _Column("fetched_at", not_null=True, default=_NOW),
            ),
            ("ontology", "normalized_query", "ontology_version"),
            identity_keyed=False,
        ),
    )


def _identity_keyed_tables() -> tuple[str, ...]:
    """Sidecars whose rows belong to a canonical row and die with it.

    ``ontology_cache`` is excluded: it caches ontology lookups, which outlive
    any particular canonical row.
    """
    return tuple(t.name for t in _sidecar_tables() if t.identity_keyed)


def _copyable(
    stored: tuple[_ColumnSignature, ...],
    expected: tuple[_ColumnSignature, ...],
    identity_keyed: bool,
) -> bool:
    """Whether stored rows can be copied positionally into the expected layout.

    Type and nullability must agree position by position; a differing DEFAULT
    does not affect values already stored, so it is not compared here.

    Names must agree too, except at the leading identity positions of an
    identity-keyed table, which is what a ``_PRIMARY_KEY`` rename changes. A
    rename is only accepted when the stored name has disappeared from the
    expected layout entirely: if it merely moved, this would be a reorder, and
    copying positionally would write each value into the wrong column.

    Approving a rename here decides the column shape, not data retention: see
    ``_migrate_sidecar_table`` for which changes actually keep their rows.
    """
    if len(stored) != len(expected):
        return False
    renameable = len(_PRIMARY_KEY) if identity_keyed else 0
    expected_names = {column[0] for column in expected}
    for position, (old, new) in enumerate(zip(stored, expected)):
        if old[1:3] != new[1:3]:
            return False
        if old[0] == new[0]:
            continue
        if position >= renameable or old[0] in expected_names:
            return False
    return True


_ALIGNMENT_REQUIRED = (
    "field_name", "ontology", "part_index", "matched_on",
    "status", "ontology_version", "matcher_version",
)
_ALIGNMENT_TERM_COLUMNS = ("term_id", "term_label", "match_score", "match_method")
_ALIGNMENT_STATUSES = frozenset({"matched", "ambiguous", "no_match"})
_ALIGNMENT_METHODS = frozenset({"exact", "synonym", "normalized_synonym", "fuzzy"})
# The acquisition vocabulary, public for the same reason as PRIMARY_KEY: the
# fill-rate report buckets by these, and a second copy would let a new tier
# be persisted here and silently dropped there.
ACQUISITION_TIERS = frozenset({"T0", "T1", "T2", "T3", "T4"})

# Non-identity columns written per sidecar row; the trailing timestamp column of
# each table is applied separately so its DDL default can stand in.
_ALIGNMENT_PAYLOAD = (
    "field_name", "ontology", "part_index", "matched_on",
    *_ALIGNMENT_TERM_COLUMNS,
    "status", "ontology_version", "matcher_version", "candidates_json",
)
_ACQUISITION_PAYLOAD = ("field_name", "tier", "source_ref")
_EVIDENCE_PAYLOAD = ("evidence_index", "field_name", "source_ref", "source_path", "source_text",
                     "normalized_value", "method")
_PENDING_PAYLOAD = ("content_hash", "reason")

# pipeline.extract_one() sets this when the LLM budget ran out with trigger
# fields still unfilled; it names a state, so it is stored as its cause.
_LEGACY_PENDING_MARKER = "pending"
_LEGACY_PENDING_REASON = "llm_budget_exhausted"

# Reason recorded when OLS4 could not be read after HttpClient exhausted its
# retries. Declared here rather than in the matcher so the store and the
# aligner cannot drift apart on the spelling.
PENDING_ONTOLOGY_UNAVAILABLE = "ontology_unavailable"

# Reason recorded when the model stopped before its structured output was
# complete. Named here for the same reason as the one above: the extractor
# raises it and the store defers on it, so one spelling serves both.
PENDING_LLM_TRUNCATED = "llm_truncated"
PENDING_LLM_INVALID_RESPONSE = "llm_invalid_response"
PENDING_LLM_REVIEW = "llm_review_pending"
PENDING_PUBLICATIONS = "publications_incomplete"

# Pending reasons that forbid committing the canonical row at all. Committing
# would stamp content_hash and profile_fingerprint, after which should_skip()
# returns True and the sample is never revisited — the work would be lost
# silently. A partial extraction (llm_budget_exhausted) is still worth keeping,
# so it is not listed.
_DEFERRING_PENDING_REASONS = frozenset({
    PENDING_ONTOLOGY_UNAVAILABLE,
    PENDING_LLM_TRUNCATED,
    PENDING_LLM_INVALID_RESPONSE,
    PENDING_LLM_REVIEW,
    PENDING_PUBLICATIONS,
})


def defers_canonical_write(status: Any) -> bool:
    """Whether a ``_status`` payload forbids committing the canonical row.

    Public so a producer can stop work it is about to throw away, without
    reaching into the reason set or restating which reasons defer.
    """
    return isinstance(status, dict) and status.get("reason") in _DEFERRING_PENDING_REASONS


def _copied_entries(payload: Any, key: str) -> list[dict[str, Any]]:
    """Shallow-copy a payload list so validation never edits the caller's dicts.

    ``None`` is an explicit empty set. Every other non-list is a producer bug
    and must raise rather than be read as one: silently treating ``{}`` as an
    empty list would delete the identity's existing rows.
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError(f"{key} must be a list of dicts, got {type(payload).__name__}")
    entries = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ValueError(f"{key}[{index}] must be a dict, got {type(entry).__name__}")
        entries.append(dict(entry))
    return entries


def _missing(entry: dict[str, Any], required: Sequence[str]) -> list[str]:
    return [name for name in required if entry.get(name) is None]


def _validated_audit(payload: Any) -> list[dict[str, Any]]:
    entries = _copied_entries(payload, "_audit_log")
    for index, entry in enumerate(entries):
        absent = _missing(entry, ("column", "rejected_value"))
        if absent:
            raise ValueError(f"_audit_log[{index}] missing keys: {absent!r}")
        # An explicit None must be defaulted too, not just a missing key: stage
        # is NOT NULL, and the audit insert's OR IGNORE would drop the row
        # silently rather than report the violation.
        if entry.get("stage") is None:
            entry["stage"] = "vocab_validation"
    return entries


def _validated_alignments(payload: Any) -> list[dict[str, Any]]:
    """Check the invariants that make the alignment sidecar joinable.

    A term ID asserted on a non-match is what makes the table unusable, so the
    status/term-nullability contract is enforced before anything is written.
    """
    entries = _copied_entries(payload, "_ontology_alignments")
    for index, entry in enumerate(entries):
        label = f"_ontology_alignments[{index}]"
        absent = _missing(entry, _ALIGNMENT_REQUIRED)
        if absent:
            raise ValueError(f"{label} missing keys: {absent!r}")
        part_index = entry["part_index"]
        if isinstance(part_index, bool) or not isinstance(part_index, int):
            raise ValueError(f"{label}.part_index must be an int, got {part_index!r}")
        if part_index < 0:
            raise ValueError(f"{label}.part_index must be non-negative, got {part_index}")
        status = entry["status"]
        if status not in _ALIGNMENT_STATUSES:
            raise ValueError(
                f"{label}.status must be one of {sorted(_ALIGNMENT_STATUSES)}, "
                f"got {status!r}"
            )
        term_values = [entry.get(name) for name in _ALIGNMENT_TERM_COLUMNS]
        if status == "matched":
            if any(value is None for value in term_values):
                raise ValueError(
                    f"{label} status='matched' requires all of "
                    f"{list(_ALIGNMENT_TERM_COLUMNS)} to be non-NULL"
                )
            if entry["match_method"] not in _ALIGNMENT_METHODS:
                raise ValueError(
                    f"{label}.match_method must be one of "
                    f"{sorted(_ALIGNMENT_METHODS)}, got {entry['match_method']!r}"
                )
            entry["match_score"] = _alignment_score(label, entry["match_score"])
        elif any(value is not None for value in term_values):
            raise ValueError(
                f"{label} status={status!r} requires all of "
                f"{list(_ALIGNMENT_TERM_COLUMNS)} to be NULL"
            )
    return entries


def _alignment_score(label: str, score: Any) -> float:
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError(f"{label}.match_score must be a number, got {score!r}")
    if not math.isfinite(score):
        raise ValueError(f"{label}.match_score must be finite, got {score!r}")
    return float(score)


def _validated_acquisitions(payload: Any) -> list[dict[str, Any]]:
    entries = _copied_entries(payload, "_field_acquisition")
    for index, entry in enumerate(entries):
        label = f"_field_acquisition[{index}]"
        absent = _missing(entry, ("field_name", "tier"))
        if absent:
            raise ValueError(f"{label} missing keys: {absent!r}")
        if entry["tier"] not in ACQUISITION_TIERS:
            raise ValueError(
                f"{label}.tier must be one of {sorted(ACQUISITION_TIERS)}, "
                f"got {entry['tier']!r}"
            )
    return entries


def _normalized_pending(payload: Any, content_hash: Any) -> dict[str, Any] | None:
    """Accept the structured pending payload or the pipeline's bare marker.

    ``extract_one()`` still sets ``_status = "pending"`` as a plain string and
    ``cli/main.py`` upserts without stripping it. Only that exact marker is
    accepted: mapping any string through to ``reason`` would let a future
    ``_status = "complete"`` write a live pending row, and ``reason`` is a cause
    rather than a state, so the marker is translated to what it actually means.
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        if payload != _LEGACY_PENDING_MARKER:
            raise ValueError(
                f"_status string must be {_LEGACY_PENDING_MARKER!r}; pass a dict "
                f"with an explicit 'reason' instead of {payload!r}"
            )
        return {"content_hash": content_hash, "reason": _LEGACY_PENDING_REASON}
    if not isinstance(payload, dict):
        raise ValueError(f"_status must be a dict, str, or None, got {type(payload).__name__}")
    status = dict(payload)
    if status.get("reason") is None:
        raise ValueError("_status missing key: 'reason'")
    status.setdefault("content_hash", content_hash)
    return status

_CacheKey = tuple[Path, str]
_SCHEMA_CACHE: dict[_CacheKey, ProfileSchema] = {}
_COLUMN_CACHE: dict[_CacheKey, tuple[str, ...]] = {}
_ENUM_CACHE: dict[_CacheKey, frozenset[str]] = {}


def _profile_snapshot(
    profile_dir: Path | None,
) -> tuple[_CacheKey, ProfileSchema]:
    """Resolve a profile to its cache key and schema.

    Hashing ``schema.yaml`` means an in-place edit invalidates the cache entry
    without an explicit ``reset_column_cache()``. The schema is parsed from the
    same bytes that were hashed, so a digest never labels a different schema.
    """
    resolved = Path(profile_dir or _DEFAULT_PROFILE_DIR).resolve(strict=False)
    raw = (resolved / "schema.yaml").read_bytes()
    cache_key = (resolved, hashlib.sha256(raw).hexdigest())
    schema = _SCHEMA_CACHE.get(cache_key)
    if schema is None:
        schema = _SCHEMA_CACHE.setdefault(cache_key, parse_profile_schema(raw))
    return cache_key, schema


def _profile_columns(
    profile_dir: Path | None,
) -> tuple[ProfileSchema, tuple[str, ...], frozenset[str]]:
    cache_key, schema = _profile_snapshot(profile_dir)
    columns = _COLUMN_CACHE.setdefault(cache_key, tuple(schema.column_names))
    enum_columns = _ENUM_CACHE.setdefault(
        cache_key,
        frozenset(c.name for c in schema.columns if c.type == "enum"),
    )
    return schema, columns, enum_columns


def get_canonical_columns(profile_dir: Path | None = None) -> tuple[str, ...]:
    _, columns, _ = _profile_columns(profile_dir)
    return columns


def get_enum_columns(profile_dir: Path | None = None) -> frozenset[str]:
    _, _, enum_columns = _profile_columns(profile_dir)
    return enum_columns


def reset_column_cache() -> None:
    _SCHEMA_CACHE.clear()
    _COLUMN_CACHE.clear()
    _ENUM_CACHE.clear()


_NUMERIC_SQLITE_TYPES = {"integer": "INTEGER", "real": "REAL"}


def _resolve_sqlite_type(column: ColumnDef) -> str:
    """Map a column definition to its SQLite type; non-numeric columns are TEXT."""
    if not column.number or not column.numeric_kind:
        return "TEXT"
    return _NUMERIC_SQLITE_TYPES.get(column.numeric_kind, "TEXT")


def _column_schema(column: ColumnDef) -> dict[str, str | bool]:
    return {
        "name": column.name,
        "sqlite_type": _resolve_sqlite_type(column),
        "not_null": column.name in _PRIMARY_KEY,
    }


def _meta_column_schema(name: str) -> dict[str, str | bool]:
    return {"name": name, "sqlite_type": "TEXT", "not_null": False}


def _compute_schema_fingerprint(columns: Sequence[ColumnDef]) -> str:
    """Fingerprint the physical table shape.

    Hashing names alone cannot detect a TEXT->INTEGER change, so type and
    nullability are part of the payload.
    """
    payload = {
        "columns": [_column_schema(column) for column in columns],
        "primary_key": list(_PRIMARY_KEY),
        "metadata_columns": [_meta_column_schema(name) for name in _META_COLUMNS],
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


_SQLITE_INT_RANGE = (SQLITE_INT_MIN, SQLITE_INT_MAX)


def _bind_numeric(column: str, numeric_kind: str, value: Any) -> int | float | None:
    """Bind a value to an INTEGER/REAL column, or refuse it.

    A non-blank string here means numeric coercion never ran. SQLite would
    happily store it as TEXT in a numeric column, after which the column sorts
    lexicographically, so it is rejected instead of silently persisted.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"Numeric column {column!r} requires int or float, got "
            f"{type(value).__name__} ({value!r}); run numeric coercion first"
        )
    if isinstance(value, float) and not math.isfinite(value):
        # SQLite stores inf as REAL infinity and silently turns NaN into NULL;
        # both are corrupt data that coercion would have rejected.
        raise ValueError(f"Numeric column {column!r} cannot store {value!r}")
    if not isinstance(value, float) and not _SQLITE_INT_RANGE[0] <= value <= _SQLITE_INT_RANGE[1]:
        raise ValueError(
            f"Numeric column {column!r} value {value!r} exceeds SQLite's 64-bit INTEGER range"
        )
    if numeric_kind == "integer" and isinstance(value, float):
        if not value.is_integer():
            raise ValueError(
                f"Numeric column {column!r} is INTEGER but got non-integral {value!r}"
            )
        return int(value)
    return value


class SchemaMigrationRequired(RuntimeError):
    """Raised when opening a canonical DB would need a destructive rebuild."""

    def __init__(
        self,
        db_path: str | Path,
        old_fp: str | None,
        new_fp: str,
        row_count: int,
        required_flag: str = _REBUILD_FLAG,
    ) -> None:
        self.db_path = Path(db_path)
        self.old_fp = old_fp
        self.new_fp = new_fp
        self.row_count = row_count
        self.required_flag = required_flag
        super().__init__(
            f"Canonical schema migration required for {self.db_path} "
            f"({row_count} rows, fingerprint {old_fp!r} -> {new_fp!r}); "
            f"re-run with {required_flag}"
        )


@dataclass
class _StoredSchemaState:
    """The canonical-table state observed on an already-open connection."""

    canonical_exists: bool
    stored_fp: str | None
    row_count: int

    def needs_rebuild(self, expected_fp: str) -> bool:
        return self.canonical_exists and self.stored_fp != expected_fp

    def needs_permission(self, expected_fp: str) -> bool:
        """An empty unversioned table is repaired silently; anything else asks."""
        return self.needs_rebuild(expected_fp) and (
            self.stored_fp is not None or self.row_count > 0
        )


def _inspect_stored_schema(conn: sqlite3.Connection) -> _StoredSchemaState:
    def table_exists(name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    if not table_exists("canonical"):
        return _StoredSchemaState(canonical_exists=False, stored_fp=None, row_count=0)

    row_count = conn.execute("SELECT COUNT(*) FROM canonical").fetchone()[0]
    stored_fp: str | None = None
    if table_exists("_schema_meta"):
        row = conn.execute(
            "SELECT value FROM _schema_meta WHERE key = 'canonical_schema_fp'"
        ).fetchone()
        stored_fp = row[0] if row else None
    return _StoredSchemaState(canonical_exists=True, stored_fp=stored_fp, row_count=row_count)


class CanonicalStore:
    def __init__(
        self,
        db_path: str | Path = "data/canonical.sqlite",
        profile_dir: Path | None = None,
        allow_destructive_rebuild: bool = False,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._schema, self._columns, self._enum_columns = _profile_columns(profile_dir)
        self._numeric_columns = {
            c.name: c.numeric_kind
            for c in self._schema.columns
            if c.number and c.numeric_kind
        }
        self._allow_destructive_rebuild = allow_destructive_rebuild
        self._preflight_migration_permission()
        self._conn = open_db(str(self._path))
        self._conn.isolation_level = None
        try:
            self._ensure_table()
        except Exception:
            self._conn.close()
            raise

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    @property
    def export_headers(self) -> list[str]:
        """Contract headers for :attr:`columns`, position for position.

        A column may publish under an alias (``RawSize_GB`` ships as
        ``RawSize(GB)``), so an export cannot reuse the internal keys as headers.
        """
        return [column.export_name for column in self._schema.columns]

    @property
    def tiered_columns(self) -> frozenset[str]:
        """The columns the schema routes through the acquisition cascade.

        Published because a reader of ``field_acquisition`` has to tell a row
        the schema sanctions from one left by a producer that wrote provenance
        for a column no tier can reach.
        """
        return frozenset(
            column.name for column in self._schema.columns
            if column.acquisition == "tiered"
        )

    def _preflight_migration_permission(self) -> None:
        """Refuse an unpermitted rebuild before the file is opened for writing.

        ``open_db()`` sets ``journal_mode=WAL``, which writes to the database, so
        the check runs over a read-only connection to keep the file untouched
        when the answer is "no".
        """
        if self._allow_destructive_rebuild or not self._path.is_file():
            return

        state = self._probe_stored_schema()
        if state is None:
            return

        expected_fp = _compute_schema_fingerprint(self._schema.columns)
        if state.needs_permission(expected_fp) and not self._publication_upgrade(state):
            raise self._migration_required(state, expected_fp)

    def _publication_upgrade(self, state: _StoredSchemaState) -> bool:
        """Recognize only the lossless v3 INTEGER PMID -> v4 TEXT-list change."""
        if not self._schema.review_policy:
            return False
        pmid = next((c for c in self._schema.columns if c.name == "Pubmed ID"), None)
        if pmid is None or pmid.number:
            return False
        old_columns = [
            c.model_copy(update={"number": True, "numeric_kind": "integer"})
            if c.name == "Pubmed ID" else c
            for c in self._schema.columns
        ]
        return state.stored_fp == _compute_schema_fingerprint(old_columns)

    def _probe_stored_schema(self) -> _StoredSchemaState | None:
        """Inspect the stored schema without applying ``open_db()``'s pragmas.

        A read-only connection is preferred. It can legitimately fail for a WAL
        database whose ``-shm`` file is missing, so a plain read-write
        connection is the fallback; neither issues anything but SELECTs.
        """
        read_only_uri = f"{self._path.resolve(strict=False).as_uri()}?mode=ro"
        for target, use_uri in ((read_only_uri, True), (str(self._path), False)):
            try:
                conn = sqlite3.connect(target, uri=use_uri)
            except sqlite3.Error:
                continue
            try:
                return _inspect_stored_schema(conn)
            except sqlite3.Error:
                continue
            finally:
                conn.close()
        logger.warning("could not inspect stored canonical schema at %s", self._path)
        return None

    def _ensure_table(self) -> None:
        expected_fp = _compute_schema_fingerprint(self._schema.columns)
        state = _inspect_stored_schema(self._conn)

        if not state.canonical_exists:
            self._initialize_schema(expected_fp)
            return

        if not state.needs_rebuild(expected_fp):
            self._assert_schema_pragma()
            self._ensure_sidecar_tables()
            return

        if self._publication_upgrade(state):
            self._upgrade_publication_ids(expected_fp)
            return

        if not self._allow_destructive_rebuild and state.needs_permission(expected_fp):
            raise self._migration_required(state, expected_fp)

        self._transactional_rebuild(expected_fp)

    def _upgrade_publication_ids(self, expected_fp: str) -> None:
        """Carry every row and sidecar through the publication type widening."""
        staging = "canonical__publication_upgrade"
        with self._transaction():
            if self._table_exists(staging):
                raise RuntimeError(f"Migration staging table {staging!r} already exists")
            self._conn.execute(f"ALTER TABLE canonical RENAME TO {_quote(staging)}")
            self._create_canonical_table()
            columns = [*self._columns, *_META_COLUMNS]
            target = ", ".join(_quote(c) for c in columns)
            source = ", ".join(
                f"CAST({_quote(c)} AS TEXT)" if c == "Pubmed ID" else _quote(c)
                for c in columns
            )
            self._conn.execute(f"INSERT INTO canonical ({target}) SELECT {source} FROM {_quote(staging)}")
            self._conn.execute(f"DROP TABLE {_quote(staging)}")
            self._update_schema_fingerprint(expected_fp)
            # The publication migration already owns the write transaction.
            # Sidecar creation/repair must commit or roll back with it.
            self._ensure_sidecar_tables_locked()
            self._assert_schema_pragma()

    def _table_exists(self, table_name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone() is not None

    def _migration_required(
        self,
        state: _StoredSchemaState,
        expected_fp: str,
    ) -> SchemaMigrationRequired:
        return SchemaMigrationRequired(
            self._path, state.stored_fp, expected_fp, state.row_count
        )

    def _create_meta_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS _schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

    def _stored_layout(
        self, table_name: str
    ) -> tuple[tuple[_ColumnSignature, ...], tuple[str, ...]]:
        rows = self._conn.execute(f"PRAGMA table_info({_quote(table_name)})").fetchall()
        signature = tuple(
            (
                str(row[1]),
                str(row[2]).upper(),
                bool(row[3]),
                _normalized_default(row[4]),
            )
            for row in rows
        )
        primary_key = tuple(
            name for _, name in sorted(
                (int(row[5]), str(row[1])) for row in rows if row[5]
            )
        )
        return signature, primary_key

    def _sidecars_need_repair(self) -> bool:
        return any(
            not self._table_exists(table.name)
            or self._stored_layout(table.name) != table.layout()
            for table in _sidecar_tables()
        )

    def _ensure_sidecar_tables(self) -> None:
        """Create or repair the sidecars, opening a write transaction only if needed."""
        if not self._sidecars_need_repair():
            return
        with self._transaction():
            self._ensure_sidecar_tables_locked()

    def _ensure_sidecar_tables_locked(self) -> None:
        for table in _sidecar_tables():
            self._ensure_sidecar_table(table)

    def _ensure_sidecar_table(self, table: _SidecarTable) -> None:
        if not self._table_exists(table.name):
            self._conn.execute(table.ddl())
            return

        expected_signature, expected_pk = table.layout()
        stored_signature, stored_pk = self._stored_layout(table.name)
        if (stored_signature, stored_pk) == (expected_signature, expected_pk):
            return

        if _copyable(stored_signature, expected_signature, table.identity_keyed):
            self._migrate_sidecar_table(table, stored_signature)
            return

        row_count = self._conn.execute(
            f"SELECT COUNT(*) FROM {_quote(table.name)}"
        ).fetchone()[0]
        if row_count:
            raise RuntimeError(
                f"cannot migrate populated sidecar table {table.name!r} "
                f"({row_count} row(s)): stored columns {stored_signature!r} are not "
                f"compatible with {expected_signature!r}"
            )
        self._conn.execute(f"DROP TABLE {_quote(table.name)}")
        self._conn.execute(table.ddl())

    def _migrate_sidecar_table(
        self,
        table: _SidecarTable,
        stored_signature: tuple[_ColumnSignature, ...],
    ) -> None:
        """Rebuild a sidecar in place, carrying whatever rows it still holds.

        SQLite cannot add or rename a composite primary key, so the table is
        renamed aside and copied back column-for-column. ``INSERT OR IGNORE``
        collapses rows the old PK-less audit table allowed to repeat.

        Rows actually survive only for a same-identity change, which is what
        gives ``extraction_audit`` its primary key without discarding history.
        An identity *rename* also reaches here, but the primary key is part of
        the canonical fingerprint, so a rename always arrives via
        ``_transactional_rebuild()``, which has already cleared these rows: for
        that case this restores the column shape, not the data.
        """
        staging = f"{table.name}__migrating"
        if self._table_exists(staging):
            # Unreachable from this code: the whole migration is one transaction,
            # so a crash rolls the staging table away. Reaching it means someone
            # else created it, and dropping it could be the only copy of rows.
            raise RuntimeError(
                f"cannot migrate sidecar table {table.name!r}: staging table "
                f"{staging!r} already exists; inspect and remove it manually"
            )
        self._conn.execute(
            f"ALTER TABLE {_quote(table.name)} RENAME TO {_quote(staging)}"
        )
        self._conn.execute(table.ddl())
        target = ", ".join(_quote(c.name) for c in table.columns)
        source = ", ".join(_quote(column[0]) for column in stored_signature)
        self._conn.execute(
            f"INSERT OR IGNORE INTO {_quote(table.name)} ({target}) "
            f"SELECT {source} FROM {_quote(staging)}"
        )
        self._conn.execute(f"DROP TABLE {_quote(staging)}")

    def _create_canonical_table(self) -> None:
        definitions: list[str] = []
        for column in self._schema.columns:
            spec = _column_schema(column)
            not_null = " NOT NULL" if spec["not_null"] else ""
            definitions.append(
                f'{self._quote_identifier(str(spec["name"]))} '
                f'{spec["sqlite_type"]}{not_null}'
            )
        definitions.extend(
            f'{self._quote_identifier(name)} TEXT' for name in _META_COLUMNS
        )
        primary_key = ", ".join(self._quote_identifier(name) for name in _PRIMARY_KEY)
        definitions.append(f"PRIMARY KEY ({primary_key})")
        self._conn.execute(
            "CREATE TABLE canonical (\n    " + ",\n    ".join(definitions) + "\n)"
        )

    _quote_identifier = staticmethod(_quote)

    def _update_schema_fingerprint(self, expected_fp: str) -> None:
        cursor = self._conn.execute(
            "UPDATE _schema_meta SET value = ? WHERE key = 'canonical_schema_fp'",
            (expected_fp,),
        )
        if cursor.rowcount == 0:
            self._conn.execute(
                "INSERT INTO _schema_meta (key, value) VALUES ('canonical_schema_fp', ?)",
                (expected_fp,),
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run a write under BEGIN IMMEDIATE, rolling back on any failure.

        COMMIT is inside the guard: a failing COMMIT must not leave the shared
        connection stuck inside a transaction.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _initialize_schema(self, expected_fp: str) -> None:
        with self._transaction():
            self._create_meta_table()
            self._ensure_sidecar_tables_locked()
            self._create_canonical_table()
            self._update_schema_fingerprint(expected_fp)
            self._assert_schema_pragma()

    def _transactional_rebuild(self, new_fp: str) -> None:
        """Replace the canonical table and clear its sidecars, atomically.

        ``raw_*``, ``llm_cache`` and ``run_log`` are independent of the canonical
        row set and are never read here. ``ontology_cache`` keeps its rows for
        the same reason, but its shape is still verified alongside the other
        sidecars, so an incompatible one can be recreated or refuse the rebuild.
        """
        with self._transaction():
            # The state is re-read under the write lock: another writer may have
            # migrated, populated, or dropped the table since the preflight.
            state = _inspect_stored_schema(self._conn)
            if state.canonical_exists and not state.needs_rebuild(new_fp):
                self._assert_schema_pragma()
                self._ensure_sidecar_tables_locked()
                return
            if not self._allow_destructive_rebuild and state.needs_permission(new_fp):
                raise self._migration_required(state, new_fp)

            if state.canonical_exists:
                logger.warning(
                    "rebuilding canonical table at %s; discarding %d row(s) "
                    "(stored fingerprint=%r expected=%s)",
                    self._path, state.row_count, state.stored_fp, new_fp,
                )
                self._conn.execute("DROP TABLE canonical")
                for table_name in _identity_keyed_tables():
                    if self._table_exists(table_name):
                        self._conn.execute(f"DELETE FROM {_quote(table_name)}")

            if not self._table_exists("_schema_meta"):
                self._create_meta_table()
            self._ensure_sidecar_tables_locked()
            self._create_canonical_table()
            self._update_schema_fingerprint(new_fp)
            self._assert_schema_pragma()

    def _assert_schema_pragma(self) -> None:
        pk_position = {name: index for index, name in enumerate(_PRIMARY_KEY, start=1)}
        expected = [
            {**spec, "pk": pk_position.get(str(spec["name"]), 0)}
            for spec in (
                [_column_schema(c) for c in self._schema.columns]
                + [_meta_column_schema(name) for name in _META_COLUMNS]
            )
        ]
        actual = [
            {
                "name": row[1],
                "sqlite_type": str(row[2]).upper(),
                "not_null": bool(row[3]),
                "pk": row[5],
            }
            for row in self._conn.execute("PRAGMA table_info(canonical)").fetchall()
        ]
        if actual != expected:
            raise RuntimeError(
                "canonical table schema does not match the active profile: "
                f"expected {expected!r}, got {actual!r}"
            )

    def upsert(self, record: dict[str, Any]) -> bool:
        """Write a canonical row and its sidecar rows in one transaction.

        The record is never mutated: payload keys are read from a shallow copy,
        so a caller can retry or compare the dict it passed in.

        A payload key that is present but empty clears that sidecar set for the
        identity; an absent key leaves it alone. That distinction is what lets
        an ontology-only re-alignment pass survive a later plain upsert.

        Returns:
            ``True`` when the canonical row was committed. ``False`` when the
            record carried a deferring pending reason: only the pending row is
            written, so the sample stays eligible for a later pass.
        """
        working = dict(record)
        absent_identity = [name for name in _PRIMARY_KEY if not working.get(name)]
        if absent_identity:
            raise ValueError(f"Record missing required keys: {absent_identity!r}")

        all_cols = self._columns + _META_COLUMNS
        for key in working:
            if key in all_cols or key in _ACCEPTED_META_KEYS:
                continue
            if isinstance(key, str) and key.startswith("_"):
                raise ValueError(
                    f"Unknown underscore key: {key!r}; accepted: "
                    f"{sorted(_ACCEPTED_META_KEYS)}"
                )
            raise ValueError(f"Unknown column key: {key!r}")

        for meta_key in _REQUIRED_META_COLUMNS:
            if not working.get(meta_key):
                raise ValueError(f"Record missing required metadata key: {meta_key!r}")

        payloads = {key: working.pop(key) for key in _ACCEPTED_META_KEYS if key in working}
        absent_fields = [c for c in self._columns if working.get(c) == "null"]
        identity = tuple(
            self._bind_value(name, None if name in absent_fields else working.get(name))
            for name in _PRIMARY_KEY
        )

        audit_entries = _validated_audit(payloads.get("_audit_log"))
        alignments = (
            _validated_alignments(payloads["_ontology_alignments"])
            if "_ontology_alignments" in payloads else None
        )
        acquisitions = (
            _validated_acquisitions(payloads["_field_acquisition"])
            if "_field_acquisition" in payloads else None
        )
        evidence = None
        if "_field_evidence" in payloads:
            evidence = _copied_entries(payloads["_field_evidence"], "_field_evidence")
            for index, entry in enumerate(evidence):
                entry["evidence_index"] = index
                if any(not isinstance(entry.get(k), str) for k in _EVIDENCE_PAYLOAD[1:]):
                    raise ValueError("field evidence requires string source, value and method fields")
                if entry["field_name"] not in self._columns:
                    raise ValueError(f"Unknown evidence field: {entry['field_name']}")
        # The bound value, so a pending row's content_hash joins against the
        # canonical column instead of differing by type.
        pending = _normalized_pending(
            payloads.get("_status"), self._bind_value("content_hash", working["content_hash"])
        )

        if pending and pending["reason"] in _DEFERRING_PENDING_REASONS:
            logger.warning(
                "deferring canonical write for %r: %s%s", identity, pending["reason"],
                f" ({pending['detail']})" if pending.get("detail") else "",
            )
            with self._transaction():
                self._replace_sidecar_rows(
                    "extraction_pending", identity, [pending], _PENDING_PAYLOAD,
                    "updated_at",
                )
            return False

        # Deferred records stop before numeric coercion in the pipeline. Bind
        # their fields only when committing a canonical row; raw numeric text
        # must not prevent recording the pending reason that makes them retryable.
        bound = {c: self._bind_value(c, None if c in absent_fields else working.get(c)) for c in all_cols}
        col_names = ", ".join(_quote(c) for c in all_cols)
        placeholders = ", ".join("?" for _ in all_cols)
        conflict = ", ".join(_quote(name) for name in _PRIMARY_KEY)
        update_set = ", ".join(
            f"{_quote(c)} = excluded.{_quote(c)}"
            for c in all_cols if c not in _PRIMARY_KEY
        )

        with self._transaction():
            self._conn.execute(
                f"INSERT INTO canonical ({col_names}) VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict}) DO UPDATE SET {update_set}",
                [bound[c] for c in all_cols],
            )
            self._replace_sidecar_rows(
                "field_absence", identity,
                [{"field_name": name} for name in absent_fields],
                ("field_name",), "recorded_at",
            )
            for entry in audit_entries:
                self._insert_audit_row(
                    identity, entry["column"], entry["rejected_value"], entry["stage"]
                )
            if alignments is not None:
                self._replace_sidecar_rows(
                    "ontology_alignment", identity, alignments, _ALIGNMENT_PAYLOAD, "aligned_at"
                )
            if acquisitions is not None:
                self._replace_sidecar_rows(
                    "field_acquisition", identity, acquisitions, _ACQUISITION_PAYLOAD, "acquired_at"
                )
            if evidence is not None:
                self._replace_sidecar_rows(
                    "field_evidence", identity, evidence, _EVIDENCE_PAYLOAD, "recorded_at"
                )
            # Pending is the one sidecar that clears on an absent payload: a
            # committed canonical row is the successful outcome that resolves
            # pendingness. Leaving it alone would make the row permanent, since
            # no producer ever sends _status=None to retract one.
            self._replace_sidecar_rows(
                "extraction_pending", identity,
                [pending] if pending else [], _PENDING_PAYLOAD, "updated_at",
            )
        return True

    def _replace_sidecar_rows(
        self,
        table_name: str,
        identity: tuple[Any, ...],
        entries: Sequence[dict[str, Any]],
        payload_columns: Sequence[str],
        defaulted_column: str,
    ) -> None:
        """Delete this identity's rows, then insert the new set.

        Set replacement rather than upsert-per-row: a value that previously
        split into three parts and now splits into one must not leave the
        stale parts behind.
        """
        where = " AND ".join(f"{_quote(name)} = ?" for name in _PRIMARY_KEY)
        self._conn.execute(
            f"DELETE FROM {_quote(table_name)} WHERE {where}", identity
        )
        for entry in entries:
            columns = [*_PRIMARY_KEY, *payload_columns]
            values = [*identity, *(entry.get(name) for name in payload_columns)]
            # Left out when absent so the DDL default supplies the timestamp.
            if entry.get(defaulted_column) is not None:
                columns.append(defaulted_column)
                values.append(entry[defaulted_column])
            self._insert_row(table_name, columns, values)

    def _insert_audit_row(
        self, identity: tuple[Any, ...], column: str, rejected_value: Any, stage: str
    ) -> None:
        # OR IGNORE: the primary key excludes run_ts, so re-extracting a sample
        # that rejects the same value again keeps the first observation.
        self._insert_row(
            "extraction_audit",
            (*_PRIMARY_KEY, "column", "rejected_value", "stage"),
            (*identity, column, str(rejected_value), stage),
            or_ignore=True,
        )

    def _insert_row(
        self,
        table_name: str,
        columns: Sequence[str],
        values: Sequence[Any],
        *,
        or_ignore: bool = False,
    ) -> None:
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        self._conn.execute(
            f"{verb} INTO {_quote(table_name)} "
            f"({', '.join(_quote(c) for c in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values),
        )

    def _bind_value(self, column: str, value: Any) -> Any:
        """Prepare one value for binding, keeping numeric columns typed.

        Numeric columns must already hold ``int``/``float``: stringifying them
        here would store TEXT in an INTEGER/REAL column, which SQLite accepts
        and which then sorts lexicographically. A leftover raw string means the
        coercion stage was skipped, so it is refused rather than persisted.
        """
        numeric_kind = self._numeric_columns.get(column)
        if numeric_kind is not None:
            return _bind_numeric(column, numeric_kind, value)
        if value is None or value == "":
            if self._schema.review_policy:
                return "" if column in self._enum_columns else None
            return "unknown" if column in self._enum_columns else None
        return str(value)

    def _identity_predicate(self) -> str:
        return " AND ".join(f"{_quote(name)} = ?" for name in _PRIMARY_KEY)

    def get(self, source: str, sample_id: str) -> dict[str, Any] | None:
        all_cols = self._columns + _META_COLUMNS
        col_names = ", ".join(_quote(c) for c in all_cols)
        row = self._conn.execute(
            f"SELECT {col_names} FROM canonical WHERE {self._identity_predicate()}",
            (source, sample_id),
        ).fetchone()
        if row is None:
            return None
        record = dict(zip(all_cols, row))
        absent = self._conn.execute(
            f"SELECT field_name FROM field_absence WHERE {self._identity_predicate()}",
            (source, sample_id),
        ).fetchall()
        for field, in absent:
            record[field] = "null"
        return record

    def _get_meta(self, column: str, source: str, sample_id: str) -> str | None:
        row = self._conn.execute(
            f"SELECT {_quote(column)} FROM canonical WHERE {self._identity_predicate()}",
            (source, sample_id),
        ).fetchone()
        return row[0] if row else None

    def get_content_hash(self, source: str, sample_id: str) -> str | None:
        return self._get_meta("content_hash", source, sample_id)

    def is_extraction_complete(self, source: str, sample_id: str, profile_fingerprint: str) -> bool:
        """Reuse a committed snapshot when explicitly resuming the same run.

        This deliberately does not refresh source content. Normal extraction
        still checks the primary/sibling content hash. Any pending stage makes
        an existing row ineligible, including a failed update of an older row.
        """
        return self._conn.execute(
            f"SELECT 1 FROM canonical WHERE {self._identity_predicate()} "
            "AND profile_fingerprint = ? AND content_hash IS NOT NULL AND content_hash != '' "
            f"AND NOT EXISTS (SELECT 1 FROM extraction_pending WHERE {self._identity_predicate()})",
            (source, sample_id, profile_fingerprint, source, sample_id),
        ).fetchone() is not None

    def get_profile_fingerprint(self, source: str, sample_id: str) -> str | None:
        return self._get_meta("profile_fingerprint", source, sample_id)

    def record_audit(self, source: str, sample_id: str, column: str, rejected_value: str, stage: str) -> None:
        with self._transaction():
            self._insert_audit_row((source, sample_id), column, rejected_value, stage)

    def record_pending(
        self,
        source: str,
        sample_id: str,
        reason: str,
        content_hash: str | None = None,
    ) -> None:
        """Mark an identity as needing another pass, leaving canonical alone.

        The table holds one row per identity, so a new reason replaces whatever
        stood there. Replacing a *different* reason is logged: the row is the
        only record that the earlier stage was still owed work, and
        ``record_alignment`` only ever retracts its own reason.
        """
        identity = (source, sample_id)
        existing = self._conn.execute(
            f"SELECT reason FROM extraction_pending WHERE {self._identity_predicate()}",
            identity,
        ).fetchone()
        if existing and existing[0] != reason:
            logger.warning(
                "replacing pending reason %r with %r for %r",
                existing[0], reason, identity,
            )
        with self._transaction():
            self._replace_sidecar_rows(
                "extraction_pending",
                identity,
                [{"reason": reason, "content_hash": content_hash}],
                _PENDING_PAYLOAD,
                "updated_at",
            )

    def stale_ontology_rows(self, expected_fingerprint: str) -> list[dict[str, Any]]:
        """Canonical rows whose ontology fingerprint is missing or out of date.

        Materialized rather than streamed: re-alignment updates ``canonical``
        on this same connection, and SQLite gives no guarantee about a SELECT
        still scanning a table that is being written underneath it.
        """
        all_cols = self._columns + _META_COLUMNS
        col_names = ", ".join(_quote(c) for c in all_cols)
        order = ", ".join(_quote(name) for name in _PRIMARY_KEY)
        rows = self._conn.execute(
            f"SELECT {col_names} FROM canonical "
            f'WHERE "ontology_fingerprint" IS NULL '
            f'OR "ontology_fingerprint" <> ? ORDER BY {order}',
            (expected_fingerprint,),
        ).fetchall()
        return [dict(zip(all_cols, row)) for row in rows]

    def record_alignment(
        self,
        source: str,
        sample_id: str,
        alignments: Sequence[dict[str, Any]],
        ontology_fingerprint: str,
    ) -> None:
        """Replace one identity's alignment rows and stamp its ontology fingerprint.

        ``content_hash`` and ``profile_fingerprint`` are deliberately untouched:
        a re-alignment says nothing about whether the extraction behind the row
        is current, and rewriting them would suppress a genuinely needed
        re-extraction.
        """
        entries = _validated_alignments(list(alignments))
        identity = (source, sample_id)
        with self._transaction():
            cursor = self._conn.execute(
                f'UPDATE canonical SET "ontology_fingerprint" = ? '
                f"WHERE {self._identity_predicate()}",
                (ontology_fingerprint, source, sample_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"no canonical row for {identity!r}")
            self._replace_sidecar_rows(
                "ontology_alignment", identity, entries, _ALIGNMENT_PAYLOAD, "aligned_at"
            )
            # A completed alignment answers the failure that deferred it; any
            # other pending reason belongs to a different stage and stands.
            self._conn.execute(
                f"DELETE FROM extraction_pending "
                f"WHERE {self._identity_predicate()} AND reason = ?",
                (source, sample_id, PENDING_ONTOLOGY_UNAVAILABLE),
            )

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM canonical").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
