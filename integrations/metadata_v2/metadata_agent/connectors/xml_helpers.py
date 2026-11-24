from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import xmltodict

_RUN_ACCESSION_RE = re.compile(r"^[SED]RR\d+")


def xml_to_dict(xml_text: str) -> dict[str, Any]:
    return xmltodict.parse(xml_text, force_list=None)


def run_accessions_from_run_set(run_set: Any, only_runs: bool = False) -> list[str]:
    """Extract run accessions from an SRA/ENA ``RUN_SET`` element.

    ``run_set`` is the parsed ``RUN_SET`` dict (a single RUN is coerced to a
    list). When ``only_runs`` is set, accessions are restricted to run-level
    prefixes (SRR/ERR/DRR); otherwise every accession found is returned.
    """
    if not isinstance(run_set, dict):
        return []
    runs = run_set.get("RUN", [])
    if isinstance(runs, dict):
        runs = [runs]
    accs: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        acc = run.get("@accession") or run.get("accession")
        if not isinstance(acc, str):
            continue
        if only_runs and not _RUN_ACCESSION_RE.match(acc):
            continue
        accs.append(acc)
    return accs


def normalize_raw(
    raw_dict: dict[str, Any],
    source: str,
    source_url: str,
    accession: str,
) -> dict[str, Any]:
    return {
        "accession": accession,
        "source": source,
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw_dict,
    }
