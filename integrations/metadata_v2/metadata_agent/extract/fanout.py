from __future__ import annotations

import re
from typing import Any

from metadata_agent.connectors.xml_helpers import run_accessions_from_run_set


def _cell(value: Any) -> str:
    """Normalize one CSV/TSV cell.

    ``csv.DictReader`` yields ``None`` for a column a ragged row omits and a list
    under the ``None`` key for overflow columns; neither is a value.
    """
    return value.strip() if isinstance(value, str) else ""


def _parse_sdrf_characteristics(row: dict[str, str]) -> dict[str, str]:
    chars: dict[str, str] = {}
    for key, val in row.items():
        cell = _cell(val)
        if not isinstance(key, str) or not cell:
            continue
        m = re.match(r"Characteristics\s*\[(.+?)\]", key, re.IGNORECASE)
        if m:
            chars[m.group(1).strip().lower()] = cell
    return chars


def _parse_sdrf_comments(row: dict[str, str]) -> dict[str, str]:
    """Comment tags, lowercased like characteristics so case-sensitive
    ``source_map`` paths ("comments.growth protocol") resolve regardless of how
    the submitter cased the tag."""
    comments: dict[str, str] = {}
    for key, val in row.items():
        cell = _cell(val)
        if not isinstance(key, str) or not cell:
            continue
        m = re.match(r"Comment\s*\[(.+?)\]", key, re.IGNORECASE)
        if m:
            comments[m.group(1).strip().lower()] = cell
    return comments


def _sdrf_sample_accession(row: dict[str, str]) -> str | None:
    comments = _parse_sdrf_comments(row)
    for key in ("ena_experiment", "ena_run", "arrayexpress ftp file"):
        val = comments.get(key, "")
        if val and re.match(r"^[A-Z]{3}\d+", val):
            return val
    source_name = _cell(row.get("Source Name"))
    if source_name and re.match(r"^[A-Z]{2,4}\d+", source_name):
        return source_name
    return None


def _gsa_run_accession(run_row: dict[str, str]) -> str | None:
    for key in ("Run", "run_accession", "Accession"):
        val = _cell(run_row.get(key))
        if val and re.match(r"^CRR\d+", val):
            return val
    return None


def _attr_key(name: str) -> str:
    """The one key convention `_attributes` uses across widened connectors.

    ``_resolve_path`` looks dict keys up case-sensitively, so a ``source_map``
    author must be able to predict the key from the source heading. SDRF and GSA
    headings are free text and their casing varies per submission.
    """
    return name.lower().replace(" ", "_")


def _sdrf_passthrough(row: dict[str, str]) -> dict[str, str]:
    """Keep the SDRF columns no typed parser claims — protocols, library, assay."""
    attrs: dict[str, str] = {}
    for key, val in row.items():
        cell = _cell(val)
        if not isinstance(key, str) or not cell:
            continue
        attr_key = _attr_key(key)
        if attr_key in ("source_name", "sample_name"):
            continue
        if re.match(r"(?:Characteristics|Comment)\s*\[", key, re.IGNORECASE):
            continue
        attrs[attr_key] = cell
    return attrs


def _sdrf_row_to_raw(row: dict[str, str], study_raw: dict[str, Any]) -> dict[str, Any]:
    characteristics = _parse_sdrf_characteristics(row)
    comments = _parse_sdrf_comments(row)

    sample_raw: dict[str, Any] = {
        "characteristics": characteristics,
        "comments": comments,
        "_attributes": _sdrf_passthrough(row),
        "source_name": _cell(row.get("Source Name")),
        "sample_name": _cell(row.get("Sample Name")),
    }

    experiment_acc = comments.get("ena_experiment", "")
    if experiment_acc:
        sample_raw["experiment_accession"] = experiment_acc

    study_info = study_raw.get("study", {})
    if isinstance(study_info, dict):
        study_acc = study_info.get("accno", "")
        if study_acc:
            sample_raw["study_accession"] = study_acc
        title = study_info.get("title", "")
        if title:
            sample_raw["title"] = title

    return sample_raw


def _gsa_run_to_raw(run_row: dict[str, str], browse_data: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, str] = {}
    for key, val in run_row.items():
        cell = _cell(val)
        if isinstance(key, str) and cell:
            attrs[_attr_key(key)] = cell

    sample_raw: dict[str, Any] = {
        "_attributes": attrs,
    }

    run_acc = _gsa_run_accession(run_row)
    if run_acc:
        sample_raw["run_accession"] = run_acc

    study_acc = _cell(run_row.get("Experiment"))
    if study_acc:
        sample_raw["study_accession"] = study_acc

    if isinstance(browse_data, dict):
        # Nested rather than merged: browse keys are study-level and must not
        # shadow the run-level fields above. `sample_ids` is excluded because it
        # lists every sample on the study page — the enrichment resolver scans
        # raw text for accessions, so keeping it would make each run adopt its
        # study-mates as siblings. The run's own sample is in `_attributes`.
        sample_raw["browse"] = {k: v for k, v in browse_data.items() if k != "sample_ids"}
        title = browse_data.get("title", "")
        if title:
            sample_raw["title"] = title

    return sample_raw


def _sra_runs_to_rows(raw: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Fan an SRA/ENA EXPERIMENT_PACKAGE_SET into one row per run accession.

    Each run inherits its experiment package's sample attributes. Falls back to
    an empty list when no run-level accession is present, so the caller can use
    the record as-is (e.g. sample/study-level fetches).
    """
    pkg_set = raw.get("EXPERIMENT_PACKAGE_SET")
    if not isinstance(pkg_set, dict):
        return []
    packages = pkg_set.get("EXPERIMENT_PACKAGE", [])
    if isinstance(packages, dict):
        packages = [packages]

    results: list[tuple[str, dict[str, Any]]] = []
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        run_accs = run_accessions_from_run_set(pkg.get("RUN_SET", {}), only_runs=True)
        if not run_accs:
            continue
        attrs = pkg.get("_attributes")
        for run_acc in run_accs:
            sample_raw = dict(pkg)
            if isinstance(attrs, dict):
                sample_raw["_attributes"] = attrs
            results.append((run_acc, {"raw": sample_raw}))
    return results


def fanout_samples(
    row: dict[str, Any], connector_id: str
) -> list[tuple[str, dict[str, Any]]]:
    raw = row.get("raw", row)
    if not isinstance(raw, dict):
        return [(row.get("accession", ""), row)]

    if connector_id == "arrayexpress":
        sdrf_samples = raw.get("sdrf_samples", [])
        if isinstance(sdrf_samples, list) and sdrf_samples:
            results: list[tuple[str, dict[str, Any]]] = []
            for sdrf_row in sdrf_samples:
                if not isinstance(sdrf_row, dict):
                    continue
                acc = _sdrf_sample_accession(sdrf_row)
                if not acc:
                    continue
                sample_raw = _sdrf_row_to_raw(sdrf_row, raw)
                results.append((acc, {"raw": sample_raw}))
            if results:
                return results

    elif connector_id == "gsa":
        run_info = raw.get("run_info", [])
        if isinstance(run_info, list) and run_info:
            browse_data = raw.get("browse", {})
            results = []
            for run_row in run_info:
                if not isinstance(run_row, dict):
                    continue
                acc = _gsa_run_accession(run_row)
                if not acc:
                    continue
                sample_raw = _gsa_run_to_raw(run_row, browse_data)
                results.append((acc, {"raw": sample_raw}))
            if results:
                return results

    elif connector_id in ("sra", "ena"):
        run_rows = _sra_runs_to_rows(raw)
        if run_rows:
            return run_rows

    return [(row.get("accession", ""), row)]
