from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from metadata_agent.config.loader import ColumnDef, ProfileSchema, load_profile_schema

_NUMERIC_ANNOTATIONS: dict[str, type] = {"integer": int, "real": float}


def _annotation(col: ColumnDef) -> type:
    """Python type for a column: numeric where declared, else text."""
    if col.number and col.numeric_kind:
        return _NUMERIC_ANNOTATIONS.get(col.numeric_kind, str)
    return str


def build_canonical_model(
    profile_dir: str | Path,
) -> tuple[type[BaseModel], dict[str, list[str]]]:
    profile_path = Path(profile_dir)
    schema = load_profile_schema(profile_path)
    return _model_from_schema(schema)


def _model_from_schema(
    schema: ProfileSchema,
) -> tuple[type[BaseModel], dict[str, list[str]]]:
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    vocab_refs: dict[str, list[str]] = {}

    for col in schema.columns:
        if col.system:
            annotations[col.name] = str
            defaults[col.name] = ...
            continue

        annotation = _annotation(col)
        if schema.review_policy and col.number:
            annotation = annotation | Literal["null"]
        if col.required:
            annotations[col.name] = annotation
            defaults[col.name] = ...
        else:
            annotations[col.name] = annotation | None
            defaults[col.name] = None

        if col.type in ("enum", "semi") and col.enum_ref:
            vocab_refs[col.name] = []

    ns = {"__annotations__": annotations, **defaults}
    model = type("CanonicalSample", (BaseModel,), ns)
    return model, vocab_refs
