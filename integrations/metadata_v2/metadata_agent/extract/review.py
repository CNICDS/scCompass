"""Reviewed extraction contract: evidence, list formatting and consistency."""
from __future__ import annotations

import re
from typing import Any

from metadata_agent.config.loader import ProfileSchema

ABSENT = "null"  # A positive assertion of absence, unlike None/blank.
UNKNOWN = {"unknown", "missing", "not provided", "not collected", "not available", "n/a", "na"}
NEGATIVE = {"none", "null", "not applicable", "no growth procedure was required."}
_CANCER = re.compile(r"\b(cancer|carcinoma|glioblastoma|glioma|lymphoma|leukemia|melanoma|sarcoma)\b", re.I)
_RANGE = re.compile(r"\d\s*(?:-|–|—|to)\s*\d", re.I)


def age_in_years(value: Any) -> float | None:
    """Convert an explicit chronological age; ranges/passages remain unknown.

    Days/weeks use a Julian year (365.25 days), months use 12 per year.
    Keep the raw age in the evidence rather than rounding neonatal ages to zero.
    """
    if isinstance(value, bool) or value is None:
        return None
    # NGDC uses labels such as '5 Year(s)'. Normalize the unit spelling,
    # retaining the original raw value for provenance and evidence.
    text = re.sub(r"\b(year|yr|month|mo|week|wk|day)\(s\)", r"\1s", str(value).strip(), flags=re.I)
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(years?|yrs?|y|months?|mos?|weeks?|wks?|days?|d)?(?:\s+old)?",
        text, re.I,
    )
    if not match:
        return None
    number, unit = float(match[1]), (match[2] or "years").casefold()
    if unit.startswith(("month", "mo")):
        number /= 12
    elif unit.startswith(("week", "wk")):
        number *= 7 / 365.25
    elif unit.startswith("d"):
        number /= 365.25
    return number


def source_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in source_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in source_strings(v)]
    return [str(value)] if value is not None and not isinstance(value, bool) else []


def grounded_answers(
    answers: dict[str, Any], raw_record: dict[str, Any], schema: ProfileSchema,
    diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    """Require an actual source quote before accepting a model's answer.

    This checks grounding, not entailment: the prompt and consistency checks
    still govern what a quoted passage means.
    """
    quotes = answers.get("_evidence", {})
    quotes = quotes if isinstance(quotes, dict) else {}
    raw = raw_record.get("raw", raw_record)
    strings = source_strings(raw)
    sample_strings = (
        source_strings({k: v for k, v in raw.items() if k != "_series_context"})
        if isinstance(raw, dict) else strings
    )
    sample_only = {"Age", "Gender", "Disease", "DiseaseSubtype", "DiseaseStage/Grade", "Tumor/Cancer",
                   "Infection", "Treatment", "CellSource/CellSorting", "Genotype", "Genotype_donor", "Time"}
    result = {}
    for col in schema.columns:
        if col.system or col.llm_mode == "disabled" or col.name not in answers:
            continue
        value = answers[col.name]
        if value is None or value == "" or isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        quote = quotes.get(col.name)
        scoped_strings = sample_strings if col.name in sample_only else strings
        if not isinstance(quote, str) or not quote.strip() or not any(quote in s for s in scoped_strings):
            diagnostics.append({"column": col.name, "type": "llm_unbacked", "value": str(value)})
            continue
        if col.name == "Disease" and isinstance(raw, dict):
            from metadata_agent.extract.source_evidence import explicit_diagnoses

            direct = explicit_diagnoses(raw)
            def clean(v):
                return re.sub(r"^patients?\s+with\s+", "", str(v).strip(), flags=re.I).casefold()
            if direct and clean(value) not in {clean(v) for v, _ in direct}:
                diagnostics.append({"column": col.name, "type": "llm_diagnosis_conflict", "value": str(value)})
                continue
        result[col.name] = value
    return result


def _parts(value: str) -> list[str]:
    # Do not split commas inside parentheses (e.g. a chemical name).
    parts, current, depth = [], [], 0
    for char in value:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and char in ",;，；\n":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    seen = set()
    result = []
    for part in parts:
        if part and part.casefold() not in seen:
            result.append(part)
            seen.add(part.casefold())
    return result


def normalize_review_record(record: dict[str, Any], schema: ProfileSchema) -> dict[str, Any]:
    result = dict(record)
    diagnostics = list(result.get("_diagnostics", []))

    def clear(field: str, reason: str) -> None:
        if result.get(field) not in (None, ""):
            diagnostics.append({"column": field, "type": reason, "value": str(result[field])})
            result[field] = None

    for col in schema.columns:
        value = result.get(col.name)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if col.name == "CellSource/CellSorting":
            value = re.sub(r"\s+cultured\s+in\b.*$", "", value, flags=re.I)
            value = re.sub(r",\s*[^,:]+\s+enriched\s*:\s*", ", ", value, flags=re.I)
            value = re.sub(r"\b((?:CD\d+[A-Za-z]*|EPCAM)[+-])[/\s]*(?=CD\d|EPCAM)", r"\1, ", value)
            value = re.sub(r"\bfluorescence[- ]activated cell sorting\b", "FACS", value, flags=re.I)
            value = re.sub(r"\bmagnetic[- ]activated cell sorting\b", "MACS", value, flags=re.I)
            value = re.sub(r"\bimmune cells\b", "immune cell", value, flags=re.I)
        folded = value.casefold()
        if not value or folded in UNKNOWN:
            result[col.name] = None
        elif folded in NEGATIVE:
            result[col.name] = ABSENT
        elif col.delimiter:
            result[col.name] = (col.delimiter + " ").join(_parts(value))
        else:
            result[col.name] = value

    age = result.get("Age")
    if isinstance(age, str) and age != ABSENT:
        years = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?|y)(?:\s+old)?", age, re.I)
        if years:
            result["Age"] = years.group(1)
    for field in ("AgeRange", "TimeRange"):
        value = result.get(field)
        if value and value != ABSENT and not _RANGE.search(str(value)):
            clear(field, "not_an_explicit_range")
    time = result.get("Time")
    if time and (re.search(r"\bpassage\b", str(time), re.I) or time == result.get("Age")):
        clear("Time", "not_experimental_time")
    for field in ("Genotype", "Genotype_donor"):
        if str(result.get(field, "")).casefold() in {"patient derived", "patient-derived"}:
            clear(field, "provenance_is_not_genotype")
    site, tissue = result.get("AnatomicalSite"), result.get("Tissue")
    if site and tissue and str(site).casefold() == str(tissue).casefold():
        clear("AnatomicalSite", "no_finer_anatomical_site")
    if str(site).casefold() in {"pbmc", "peripheral blood mononuclear cells"}:
        clear("AnatomicalSite", "cell_source_is_not_anatomical_site")
    if result.get("CellLine") and result.get("CellLine") == result.get("SampleName"):
        # Only ambiguous identifiers are removed; an explicitly named line may
        # also be used as the title, so retain plain conventional line names.
        if re.search(r"scRNA|_P\d|[-_]S\d", str(result["CellLine"]), re.I):
            clear("CellLine", "sample_code_is_not_cell_line")
    stage = result.get("DiseaseStage/Grade")
    if str(stage).casefold() in {"gbm", "glioblastoma", "glioblastoma multiforme"}:
        clear("DiseaseStage/Grade", "diagnosis_is_not_stage")
    disease = result.get("Disease")
    if isinstance(disease, str):
        disease = re.sub(r"^patient(?:s)?\s+with\s+", "", disease, flags=re.I)
        result["Disease"] = disease
    if str(disease).casefold() in {"control", "healthy", "normal", "healthy control"}:
        result["Disease"] = ABSENT
        result["DiseaseSubtype"] = ABSENT
    if str(result.get("Tumor/Cancer")).casefold() in {"no", "n"}:
        for field in ("Disease", "DiseaseSubtype"):
            if _CANCER.search(str(result.get(field, ""))):
                clear(field, "cancer_status_conflict")
    if result.get("Disease") == ABSENT:
        for field in ("DiseaseSubtype", "DiseaseStage/Grade"):
            if result.get(field) not in (None, "", ABSENT):
                clear(field, "disease_absent_conflict")
    for field in ("TreatmentProtocol", "GrowthProtocol"):
        if str(result.get(field, "")).casefold() == "these details are included in extraction protocol.":
            clear(field, "unresolved_protocol_reference")
    if diagnostics:
        result["_diagnostics"] = diagnostics
    return result
