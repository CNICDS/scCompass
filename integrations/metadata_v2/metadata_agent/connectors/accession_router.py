"""Accession prefix router — maps INSDC/GEO/GSA accession prefixes to the
connector that can fetch them.

Pure stdlib, no mutable state: all functions are thread-safe. Input is
normalized to uppercase before routing, so ``srr123456`` and ``SRR123456``
route identically.
"""
from __future__ import annotations

import re

# prefix -> (connector_name, accession_type)
PREFIX_MAP: dict[str, tuple[str, str]] = {
    "SRR": ("sra", "run"),
    "SRX": ("sra", "experiment"),
    "SRS": ("sra", "sample"),
    "SRP": ("sra", "study"),
    "ERR": ("ena", "run"),
    "ERX": ("ena", "experiment"),
    "ERS": ("ena", "sample"),
    "ERP": ("ena", "study"),
    "DRR": ("sra", "run"),       # DDBJ, mirrored via NCBI
    "DRX": ("sra", "experiment"),
    "DRS": ("sra", "sample"),
    "DRP": ("sra", "study"),
    "CRR": ("gsa", "run"),
    "CRA": ("gsa", "study"),
    "GSM": ("geo", "sample"),
    "GSE": ("geo", "series"),
    # BioSample — the INSDC cross-link hub. SAMN→NCBI, SAMEA/SAMD→ENA Browser,
    # SAMC→GSA/NGDC (resolved by the GSA connector).
    "SAMN": ("biosample", "sample"),
    "SAMEA": ("biosample", "sample"),
    "SAMD": ("biosample", "sample"),
    "SAMC": ("gsa", "sample"),
}

# Per-prefix format validation. Most accessions carry 6-10 digits; GEO series
# and samples run shorter (4-10) and GSA studies cap at 9. BioSample DDBJ
# (SAMD) carries an optional archive letter before the digits.
PREFIX_PATTERNS: dict[str, re.Pattern[str]] = {
    "SRR": re.compile(r"^SRR\d{6,10}$"),
    "SRX": re.compile(r"^SRX\d{6,10}$"),
    "SRS": re.compile(r"^SRS\d{6,10}$"),
    "SRP": re.compile(r"^SRP\d{6,10}$"),
    "ERR": re.compile(r"^ERR\d{6,10}$"),
    "ERX": re.compile(r"^ERX\d{6,10}$"),
    "ERS": re.compile(r"^ERS\d{6,10}$"),
    "ERP": re.compile(r"^ERP\d{6,10}$"),
    "DRR": re.compile(r"^DRR\d{6,10}$"),
    "DRX": re.compile(r"^DRX\d{6,10}$"),
    "DRS": re.compile(r"^DRS\d{6,10}$"),
    "DRP": re.compile(r"^DRP\d{6,10}$"),
    "CRR": re.compile(r"^CRR\d{6,10}$"),
    "CRA": re.compile(r"^CRA\d{6,9}$"),
    "GSM": re.compile(r"^GSM\d{4,10}$"),
    "GSE": re.compile(r"^GSE\d{4,10}$"),
    "SAMN": re.compile(r"^SAMN\d{6,10}$"),
    "SAMEA": re.compile(r"^SAMEA\d{6,10}$"),
    "SAMD": re.compile(r"^SAMD[A-Z]?\d{6,10}$"),
    "SAMC": re.compile(r"^SAMC\d{6,10}$"),
}

_LEADING_ALPHA_RE = re.compile(r"^([A-Z]+)")

# Longest-first so a multi-character prefix (SAMEA) is tested before any shorter
# prefix that is a string-prefix of it; ties broken alphabetically for stable
# error messages.
_SORTED_PREFIXES: list[str] = sorted(PREFIX_MAP, key=lambda p: (-len(p), p))

# Every routable prefix must carry a format pattern; guard against the two maps
# drifting out of sync when a prefix is added to one but not the other.
assert set(PREFIX_MAP) == set(PREFIX_PATTERNS), (
    "PREFIX_MAP and PREFIX_PATTERNS must define the same prefixes: "
    f"{set(PREFIX_MAP) ^ set(PREFIX_PATTERNS)}"
)


def route_accession(acc: str) -> tuple[str, str]:
    """Route a single accession to ``(connector_name, accession_type)``.

    Prefixes are matched longest-first (so ``SAMEA`` wins over any shorter
    string-prefix) and validated against the prefix-specific format pattern.

    Raises:
        ValueError: prefix unknown, or the numeric suffix fails the
            prefix-specific format check.
    """
    normalized = (acc or "").strip().upper()

    matched_prefix: str | None = None
    for prefix in _SORTED_PREFIXES:
        if normalized.startswith(prefix):
            if PREFIX_PATTERNS[prefix].match(normalized):
                return PREFIX_MAP[prefix]
            if matched_prefix is None:
                matched_prefix = prefix

    if matched_prefix is not None:
        raise ValueError(f"Malformed accession {acc!r} for prefix {matched_prefix!r}")

    alpha = _LEADING_ALPHA_RE.match(normalized)
    if alpha:
        raise ValueError(f"Unknown accession prefix {alpha.group(1)!r} in {acc!r}")
    raise ValueError(f"Unroutable accession: {acc!r}")


def route_batch(
    accessions: list[str],
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    """Group valid accessions by connector; collect invalid ones separately.

    Returns ``(grouped, errors)`` where ``grouped`` maps a connector name to a
    list of ``(normalized_accession, accession_type)`` pairs, and ``errors``
    maps the original accession string to a failure message. Never raises.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    errors: dict[str, str] = {}

    seen: set[str] = set()
    for acc in accessions:
        try:
            normalized = normalize_accession_input(acc)
            connector, acc_type = route_accession(normalized)
        except ValueError as exc:
            errors[acc] = str(exc)
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        grouped.setdefault(connector, []).append((normalized, acc_type))

    return grouped, errors


def normalize_accession_input(value: str) -> str:
    """Accept an accession or an h5ad basename containing one accession.

    Only the basename contributes identity; directories never do. Files need
    not exist because this is source lookup, not matrix ingestion. Ambiguous
    filenames are rejected rather than selecting one sample arbitrarily.
    """
    normalized = (value or "").strip().upper()
    if not normalized.endswith(".H5AD"):
        route_accession(normalized)
        return normalized
    basename = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    candidates = set()
    for token in re.split(r"[_.\s-]+", basename[:-5]):
        try:
            route_accession(token)
        except ValueError:
            continue
        candidates.add(token)
    if len(candidates) != 1:
        raise ValueError(f"h5ad filename must contain exactly one valid accession: {value!r}")
    return candidates.pop()
