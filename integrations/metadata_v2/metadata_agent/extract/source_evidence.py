"""Sample-scoped assertions that must survive LLM and cross-database merges.

These checks use structured attributes and short sample descriptions, never a
cohort's diagnoses or shared protocols. They are also usable for repairing an
older export without inventing replacement values for unrelated columns.
"""
from __future__ import annotations

import re
from typing import Any

from metadata_agent.config.loader import ProfileSchema
from metadata_agent.extract.review import ABSENT, NEGATIVE, age_in_years, normalize_review_record
from metadata_agent.extract.rules import _resolve_values

_MARKER = re.compile(r"\b(?:CD\d+[A-Za-z]*|EPCAM)[+-]", re.I)
_SURGERY = re.compile(r"\b(?:undergoing|underwent)\b.*\b(?:surgery|replacement|procedure)\b", re.I)
_BENIGN = re.compile(r"\b(?:non[- ]malignant|benign)\b", re.I)
_ADJACENT = re.compile(r"\b(?:adjacent normal|normal adjacent|normal tissue)\b", re.I)
_CONTROL = re.compile(r"^(?:healthy(?: donor| control)?|control|normal)$", re.I)
_DIAGNOSIS_KEYS = {"diagnosis", "disease", "disease state", "disease status", "subject status"}


def sample_attributes(raw: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (key, value, path) from the sample's own attribute containers."""
    found = []
    for container_name in ("Sample_characteristics_ch1", "_attributes", "characteristics"):
        container = raw.get(container_name)
        if isinstance(container, dict):
            entries = [(str(k), str(v)) for k, v in container.items() if isinstance(v, (str, int, float))]
        elif isinstance(container, list):
            entries = [tuple(v.split(":", 1)) for v in container if isinstance(v, str) and ":" in v]
        else:
            entries = []
        found.extend((k.strip().casefold(), v.strip(), f"{container_name}.{k.strip()}") for k, v in entries)
    return found


def explicit_diagnoses(raw: dict[str, Any]) -> list[tuple[str, str]]:
    candidates = [(value, path) for key, value, path in sample_attributes(raw)
                  if key in _DIAGNOSIS_KEYS and value and not _SURGERY.search(value)]
    # A diagnosis is more specific than a study's subject-status label.
    primary = [(value, path) for value, path in candidates if path.endswith(".diagnosis")]
    return primary or candidates


def reconcile_sample_evidence(
    record: dict[str, Any], raw_record: dict[str, Any], schema: ProfileSchema,
    connector_id: str | None = None,
) -> dict[str, Any]:
    """Enforce direct diagnoses/age/markers and sample-specific negatives.

    Return a copy with evidence and audit diagnostics; callers normalize and
    coerce once more after this step. Unreported donor traits are not guessed.
    """
    result = dict(record)
    raw = raw_record.get("raw", raw_record)
    if not isinstance(raw, dict):
        return result
    evidence = [dict(item) for item in result.get("_field_evidence", [])]
    diagnostics = list(result.get("_diagnostics", []))
    accession = raw_record.get("accession") or result.get("SourceID") or "record"
    source_ref = raw_record.get("source_url") or f"{connector_id or 'raw'}:{accession}"
    columns = {col.name: col for col in schema.columns}

    def set_value(field: str, value: Any, path: str, quote: str, method: str = "sample_attribute") -> None:
        if field not in columns:
            return
        previous = result.get(field)
        if previous != value and previous not in (None, ""):
            diagnostics.append({"column": field, "type": "source_evidence_override", "value": str(previous)})
        result[field] = value
        item = {"field_name": field, "source_ref": str(source_ref), "source_path": path,
                "source_text": quote, "normalized_value": str(value) if value is not None else "",
                "method": method}
        if item not in evidence:
            evidence.append(item)

    # Preserve the entire marker set even when the model supplies only a label.
    sorting = columns.get("CellSource/CellSorting")
    if sorting and connector_id:
        for path in sorting.source_map.get(connector_id, []):
            for value in _resolve_values(raw, path):
                markers = _MARKER.findall(value)
                if not markers:
                    continue
                current = str(result.get(sorting.name) or "")
                present = {m.casefold() for m in _MARKER.findall(current)}
                missing = list(dict.fromkeys(m for m in markers if m.casefold() not in present))
                if missing:
                    if current.casefold() in {ABSENT, "unknown", "n/a"}:
                        current = ""
                    current = ", ".join([part for part in [current, *missing] if part])
                set_value(sorting.name, current, path, value, "preserve_sorting_markers")

    diagnoses = explicit_diagnoses(raw)
    if diagnoses:
        values = []
        for value, _ in diagnoses:
            value = re.sub(r"^patients?\s+with\s+", "", value, flags=re.I)
            if value not in values:
                values.append(value)
        # Do not merge a contradictory healthy/control assertion into a diagnosis.
        if any(_CONTROL.fullmatch(value) for value in values) and len(values) > 1:
            set_value("Disease", None, diagnoses[0][1], "; ".join(v for v, _ in diagnoses), "diagnosis_conflict")
        else:
            set_value("Disease", ", ".join(values), diagnoses[0][1], diagnoses[0][0])
    elif _SURGERY.search(str(result.get("Disease") or "")):
        set_value("Disease", None, "sample attributes", str(result["Disease"]), "procedure_is_not_disease")

    # A negative must belong to this sample, not to another arm in a protocol.
    texts = []
    # Free-form descriptions may discuss comparator samples as well. Only the
    # sample's own labels/attributes support a deterministic negative here.
    for path in ("Sample_title", "Sample_source_name_ch1"):
        texts.extend((path, value) for value in _resolve_values(raw, path))
    texts.extend((path, value) for key, value, path in sample_attributes(raw)
                 if key in {"source_name", "tissue", "sample type", *_DIAGNOSIS_KEYS})
    negatives = [(path, value) for path, value in texts
                 if (_BENIGN.search(value) or _ADJACENT.search(value) or _CONTROL.fullmatch(value))
                 and not re.search(r"\b(?:not|no)\s+(?:benign|normal|healthy)\b", value, re.I)]
    if negatives:
        path, quote = negatives[0]
        set_value("Tumor/Cancer", "no", path, quote, "sample_negative")

    # Structured chronological age has precedence over inferred/model values.
    age = columns.get("Age")
    if age and connector_id:
        for path in age.source_map.get(connector_id, []):
            values = _resolve_values(raw, path)
            if values:
                years = [age_in_years(v) for v in values]
                if all(v is not None for v in years) and len(set(years)) == 1:
                    set_value("Age", years[0], path, values[0], "age_years_365.25_days_12_months")
                elif len(values) == 1:
                    original = ABSENT if values[0].casefold() in NEGATIVE else values[0]
                    set_value("Age", original, path, values[0], "unresolved_age")
                else:
                    set_value("Age", None, path, "; ".join(values), "unresolved_age")
                break
    result["_field_evidence"] = evidence
    if diagnostics:
        result["_diagnostics"] = diagnostics
    result = normalize_review_record(result, schema)
    for item in evidence:
        value = result.get(item["field_name"])
        item["normalized_value"] = str(value) if value is not None else ""
    return result
