from __future__ import annotations

import csv
import enum
import keyword
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, field_validator

Acquisition = Literal["system", "source", "free_text", "tiered", "placeholder"]
NumericKind = Literal["integer", "real"]

SYSTEM_ACQUISITION_COLUMNS = frozenset({"Source", "SourceID"})
PLACEHOLDER_ACQUISITION_COLUMNS = frozenset({"Score"})


class ColumnDef(BaseModel):
    name: str
    type: str
    enum_ref: str | None = None
    required: bool = False
    system: bool = False
    source_map: dict[str, list[str]] | None = None
    export_as: str | None = None
    acquisition: Acquisition | None = None
    delimiter: str | None = None
    number: bool = False
    numeric_kind: NumericKind | None = None
    description: str = ""
    source_merge: Literal["first", "all"] = "first"
    llm_mode: Literal["extract", "prefer", "disabled"] = "extract"

    @field_validator("type")
    @classmethod
    def valid_type(cls, v: str) -> str:
        if v not in ("string", "enum", "semi"):
            raise ValueError(f"column type must be 'string', 'enum', or 'semi', got '{v}'")
        return v

    @property
    def export_name(self) -> str:
        """Header this column exports under: the alias if set, else the internal key."""
        return self.export_as or self.name


class ProfileSchema(BaseModel):
    version: str
    columns: list[ColumnDef]
    review_policy: bool = False

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def non_system_columns(self) -> list[ColumnDef]:
        return [c for c in self.columns if not c.system]

    @property
    def enum_columns(self) -> list[ColumnDef]:
        return [c for c in self.columns if c.type in ("enum", "semi")]


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    if config_path is None:
        config_path = Path("config.yaml")
    config = load_yaml(config_path)
    _reject_unimplemented_tiers(config)
    _normalize_search_config(config)
    return config


class ConfigError(Exception):
    pass


# Declared in config.yaml so their field rosters can be reviewed, but no code
# acquires them. Enabling one is rejected here rather than at extraction time:
# silently doing nothing would read as "these fields have no data".
_UNIMPLEMENTED_TIERS = ("T2", "T3")


def _reject_unimplemented_tiers(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        return
    tiers = (config.get("extraction") or {}).get("tiers") or {}
    if not isinstance(tiers, dict):
        return
    for tier in _UNIMPLEMENTED_TIERS:
        if (tiers.get(tier) or {}).get("enabled"):
            raise ConfigError(
                f"extraction.tiers.{tier}.enabled=true is not implemented"
            )


def resolve_trigger_fields(
    config: dict[str, Any], schema: ProfileSchema
) -> list[str]:
    """The fields whose emptiness triggers LLM fallback.

    ``extraction.llm_trigger`` when configured, else every non-system
    vocabulary field. A configured name absent from the active schema is an
    error rather than a silently dead trigger: the list is the only thing
    standing between a sparse record and an LLM call, so a typo in it would
    disable fallback without any symptom.
    """
    schema_names = {col.name for col in schema.columns}
    configured = (config.get("extraction") or {}).get("llm_trigger")

    if configured is None:
        return [
            col.name for col in schema.columns
            if col.type in ("enum", "semi") and not col.system
        ]

    if not isinstance(configured, list):
        raise ConfigError(
            f"extraction.llm_trigger must be a list, got {type(configured).__name__}"
        )

    unknown = [name for name in configured if name not in schema_names]
    if unknown:
        raise ConfigError(
            f"extraction.llm_trigger names fields absent from the active schema: {unknown!r}"
        )
    return list(configured)


def _normalize_search_config(config: dict[str, Any]) -> None:
    """Normalize each database's discovery-query config in place.

    - ``search_query`` (str) is normalized to ``search_queries`` (single-element
      list); supplying both non-identical forms is a ``ConfigError``.
    - A top-level ``discovery`` block (``organism``, ``discovery_terms``) is
      propagated into each database that does not override it, so the per-
      connector query builders see the golden-口径 terms.
    """
    if not isinstance(config, dict):
        return
    discovery = config.get("discovery") or {}
    default_terms = discovery.get("discovery_terms")
    default_organism = discovery.get("organism")

    databases = config.get("databases") or {}
    for db_name, db in databases.items():
        if not isinstance(db, dict):
            continue
        db["search_queries"] = _resolve_search_queries(db_name, db)
        if "discovery_terms" not in db and default_terms is not None:
            db["discovery_terms"] = default_terms
        if "organism" not in db and default_organism is not None:
            db["organism"] = default_organism


def _resolve_search_queries(db_name: str, db: dict[str, Any]) -> list[str]:
    legacy = db.get("search_query")
    explicit = db.get("search_queries")

    legacy_list = (
        [legacy] if isinstance(legacy, str)
        else (list(legacy) if isinstance(legacy, list) else None)
    )
    explicit_list = (
        list(explicit) if isinstance(explicit, list)
        else ([explicit] if isinstance(explicit, str) else None)
    )

    if legacy_list is not None and explicit_list is not None and legacy_list != explicit_list:
        raise ConfigError(
            f"databases.{db_name}: 'search_query' {legacy_list} and 'search_queries' "
            f"{explicit_list} both set with different values"
        )
    resolved = explicit_list if explicit_list is not None else legacy_list
    return resolved or []


def parse_profile_schema(raw: bytes | str) -> ProfileSchema:
    """Parse schema YAML that a caller has already read.

    Callers that fingerprint ``schema.yaml`` use this to parse the exact bytes
    they hashed, so the digest and the schema can never disagree.
    """
    return ProfileSchema(**yaml.safe_load(raw))


def load_profile_schema(profile_dir: Path) -> ProfileSchema:
    return parse_profile_schema((profile_dir / "schema.yaml").read_bytes())


_ALIAS_SEPARATOR = "|"


def load_vocab(vocab_path: Path) -> list[str]:
    values = []
    with open(vocab_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            values.append(row["value"])
    return values


def load_vocab_aliases(vocab_path: Path) -> dict[str, str]:
    """Case-folded alias -> canonical value for one vocabulary file.

    Aliases sit beside their canonical value in the vocabulary CSV's optional
    ``aliases`` column (``|``-separated). Keeping them there rather than in a
    module table is what lets validation stay schema-driven: a field-name-keyed
    alias map in code is exactly the hardcoded list that drifts from the schema.
    """
    resolved: dict[str, str] = {}
    with open(vocab_path, newline="") as f:
        for row in csv.DictReader(f):
            value = (row.get("value") or "").strip()
            if not value:
                continue
            for alias in (row.get("aliases") or "").split(_ALIAS_SEPARATOR):
                folded = alias.strip().casefold()
                if folded:
                    resolved[folded] = value
    return resolved


def load_vocab_sets(
    profile_dir: Path, schema: ProfileSchema
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]]]:
    """Allowed values and alias lookups for every vocabulary column in ``schema``.

    One loader for all entry points: the CLI commands and the demo each parsed
    these CSVs by hand, and the copy that split rows on ``,`` would read the
    aliases column as if it were another value.
    """
    vocab_dir = profile_dir / "vocab"
    values: dict[str, set[str]] = {}
    aliases: dict[str, dict[str, str]] = {}
    for col in schema.columns:
        if not col.enum_ref:
            continue
        vocab_path = vocab_dir / col.enum_ref
        if not vocab_path.exists():
            continue
        values[col.name] = {v.strip() for v in load_vocab(vocab_path) if v and v.strip()}
        aliases[col.name] = load_vocab_aliases(vocab_path)
    return values, aliases


_SQLITE_UNSAFE = re.compile(r'[^\w\s/\-]', re.UNICODE)

# `export_as` holds a contract header written verbatim to xlsx and used as a
# double-quoted SQL alias, so it admits characters `_SQLITE_UNSAFE` rejects for
# internal keys (`RawSize(GB)`). Only quote/control characters can break either.
_EXPORT_HEADER_UNSAFE = re.compile('[\"\x00-\x1f\x7f-\x9f\u2028\u2029]')


def _is_valid_export_header(value: str) -> bool:
    return bool(value) and value == value.strip() and not _EXPORT_HEADER_UNSAFE.search(value)


def _safe_member_name(v: str) -> str:
    key = v.replace(" ", "_").replace("/", "_").replace("-", "_")
    key = "".join(
        c if c.isascii() and (c.isalnum() or c == "_")
        else f"_u{ord(c):04x}_"
        for c in key
    )
    if not key or key[0].isdigit():
        key = f"_{key}"
    if not key or all(c == "_" for c in key):
        key = f"_val_{abs(hash(v)) % 10000}"
    if keyword.iskeyword(key) or keyword.issoftkeyword(key):
        key = f"{key}_"
    return key


def generate_enum(name: str, values: list[str]) -> type[enum.Enum]:
    members: dict[str, str] = {}
    seen_keys: dict[str, str] = {}
    for v in values:
        key = _safe_member_name(v)
        if key in seen_keys:
            raise ValueError(
                f"Enum '{name}': member name collision — "
                f"'{v}' and '{seen_keys[key]}' both normalize to '{key}'"
            )
        seen_keys[key] = v
        members[key] = v
    return enum.Enum(name, members)


class ProfileValidationError(Exception):
    pass


def validate_profile_schema(
    schema: ProfileSchema,
    vocab_dir: Path,
    *,
    tiered_fields: Collection[str] | None = None,
) -> None:
    """Validate a profile schema against its vocab directory.

    ``tiered_fields`` is the set of field names the tier configuration can
    actually acquire. It is injected rather than hardcoded so the tier roster has
    a single owner (the tier config) instead of a copy here that could drift.
    A schema declaring ``acquisition: tiered`` without it fails validation rather
    than passing unchecked: an unverifiable claim is not a certified one.
    """
    if isinstance(tiered_fields, str):
        raise TypeError("tiered_fields must be a collection of names, not a single str")

    errors: list[str] = []

    names = [c.name for c in schema.columns]
    if len(names) != len(set(names)):
        dupes = [n for n in names if names.count(n) > 1]
        errors.append(f"Duplicate column names: {set(dupes)}")

    for col in schema.columns:
        if col.type in ("enum", "semi") and not col.enum_ref:
            errors.append(f"Column '{col.name}' type={col.type} but no enum_ref")
        if col.type == "string" and col.enum_ref:
            errors.append(f"Column '{col.name}' type=string but has enum_ref={col.enum_ref}")

        if col.enum_ref:
            vocab_path = vocab_dir / col.enum_ref
            if not vocab_path.exists():
                errors.append(f"Vocab file missing: {vocab_path}")
            elif vocab_path.stat().st_size == 0:
                errors.append(f"Vocab file empty: {vocab_path}")
            else:
                vals = load_vocab(vocab_path)
                if "value" in vals:
                    errors.append(f"Vocab '{col.enum_ref}': header 'value' found as member")
                if len(vals) != len(set(vals)):
                    dupes = [x for x in vals if vals.count(x) > 1]
                    errors.append(f"Vocab '{col.enum_ref}': duplicate values {set(dupes)}")
                for v in vals:
                    if not v or not v.strip():
                        errors.append(f"Vocab '{col.enum_ref}': empty or whitespace-only value")
                        break
                _check_enum_member_collisions(col.name, vals, errors)

        if _SQLITE_UNSAFE.search(col.name):
            errors.append(f"Column '{col.name}' may be unsafe as SQLite identifier")

        _check_column_metadata(col, tiered_fields, errors)

    if errors:
        raise ProfileValidationError(
            "Profile validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )


def _check_column_metadata(
    col: ColumnDef, tiered_fields: Collection[str] | None, errors: list[str]
) -> None:
    if col.acquisition == "system":
        if col.name not in SYSTEM_ACQUISITION_COLUMNS:
            errors.append(
                f"Column '{col.name}' acquisition=system is only valid for "
                f"{sorted(SYSTEM_ACQUISITION_COLUMNS)}"
            )
    elif col.acquisition == "source":
        paths = (col.source_map or {}).values()
        if not any(p.strip() for group in paths for p in group):
            errors.append(
                f"Column '{col.name}' acquisition=source requires a nonempty source_map"
            )
    elif col.acquisition == "tiered":
        if tiered_fields is None:
            errors.append(
                f"Column '{col.name}' acquisition=tiered cannot be validated: "
                "no tier configuration was supplied"
            )
        elif col.name not in tiered_fields:
            errors.append(
                f"Column '{col.name}' acquisition=tiered but field is absent from "
                "tier configuration"
            )
    elif col.acquisition == "placeholder":
        if col.name not in PLACEHOLDER_ACQUISITION_COLUMNS:
            errors.append(
                f"Column '{col.name}' acquisition=placeholder is only valid for "
                f"{sorted(PLACEHOLDER_ACQUISITION_COLUMNS)}"
            )

    if col.export_as is not None and not _is_valid_export_header(col.export_as):
        errors.append(
            f"Column '{col.name}' export_as={col.export_as!r} is not a valid export header"
        )

    if col.number and col.numeric_kind is None:
        errors.append(f"Column '{col.name}' number=true requires numeric_kind")
    if not col.number and col.numeric_kind is not None:
        errors.append(
            f"Column '{col.name}' numeric_kind={col.numeric_kind!r} requires number=true"
        )


def _check_enum_member_collisions(
    col_name: str, values: list[str], errors: list[str]
) -> None:
    seen: dict[str, str] = {}
    for v in values:
        key = _safe_member_name(v)
        if key in seen:
            errors.append(
                f"Column '{col_name}' vocab: '{v}' and '{seen[key]}' "
                f"both normalize to member name '{key}'"
            )
        else:
            seen[key] = v


def load_profile(
    profile_dir: Path, *, tiered_fields: Collection[str] | None = None
) -> dict[str, Any]:
    schema = load_profile_schema(profile_dir)
    vocab_dir = profile_dir / "vocab"

    validate_profile_schema(schema, vocab_dir, tiered_fields=tiered_fields)

    enums: dict[str, type[enum.Enum]] = {}

    for col in schema.columns:
        if col.type in ("enum", "semi") and col.enum_ref:
            vocab_path = vocab_dir / col.enum_ref
            if vocab_path.exists():
                values = load_vocab(vocab_path)
                enums[col.name] = generate_enum(col.name, values)

    rules_path = profile_dir / "rules.yaml"
    rules = load_yaml(rules_path) if rules_path.exists() else {"rules": []}

    prompt_path = profile_dir / "prompt.md"
    prompt_template = prompt_path.read_text() if prompt_path.exists() else ""

    return {
        "schema": schema,
        "enums": enums,
        "rules": rules,
        "prompt_template": prompt_template,
    }
