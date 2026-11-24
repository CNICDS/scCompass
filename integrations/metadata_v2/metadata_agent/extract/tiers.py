"""Ordered tier cascade for the fields no source API exposes as a plain column.

T0 reads run metadata the connector already fetched, falling back to ENA's
filereport endpoint when a record names a run but carries no run-level detail.
T1 resolves the publication fields through PubMed. T2 and T3 are configured but
unimplemented: ``load_config`` rejects them, so this module only has to refuse
to invent their values.

The cascade offers a field to each enabled tier in turn and the first value that
survives the column's declared type owns it; no later tier is asked. Fields that
resolve to nothing stay absent, and only the ones that resolved get a
``_field_acquisition`` row -- a row against a NULL carries no provenance and
would inflate the fill-rate denominator.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any

from metadata_agent.config.loader import ProfileSchema
from metadata_agent.config.tiers import tiered_fields_from_config
from metadata_agent.connectors.sra import ELINK_URL, ESEARCH_URL, ESUMMARY_URL
from metadata_agent.enrich.merge import is_fillable
from metadata_agent.extract.numeric import bounds_for, coerce_number
from metadata_agent.runtime import count, timed
from metadata_agent.store.source_cache import SourceCache

logger = logging.getLogger(__name__)

ENA_FILEREPORT_URL = "https://www.ebi.ac.uk/ena/portal/api/filereport"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov"

TIER_ORDER = ("T0", "T1", "T2", "T3")

# Requested in one block because ENA rejects the whole report when a name is not
# recognised. The reader tolerates any of them being missing from the response,
# so a rename costs the fallback rather than the run.
_FILEREPORT_FIELDS = (
    "run_accession", "experiment_accession", "sample_accession", "study_accession",
    "library_name", "library_strategy", "library_source", "library_selection",
    "instrument_platform", "instrument_model", "experiment_title", "study_title",
    "fastq_ftp", "fastq_bytes", "submitted_ftp", "submitted_bytes", "first_public",
)

# Run accessions (SRR/ERR/DRR and their experiment/sample/study siblings) and the
# GEO series/sample accessions eLink understands.
_RUN_ACCESSION = re.compile(r"[SED]R[RSPX]\d+", re.IGNORECASE)
_GEO_ACCESSION = re.compile(r"GS[ME]\d+", re.IGNORECASE)
_DOI = re.compile(r"10\.\d{4,9}/\S+")
_YEAR = re.compile(r"[12]\d{3}")

# Keys whose text is direct evidence of how the library was built. Only these
# justify answering "other": a value outside the vocabulary is still an answer,
# but only when something actually described the library.
_LIBRARY_KEYS = frozenset({
    "librarystrategy", "librarysource", "libraryselection", "libraryname",
    "libraryconstructionprotocol", "librarylayout", "designdescription",
    "libraryprotocol", "instrumentmodel", "instrumentplatform",
    "sampleextractprotocolch1", "samplelibrarystrategy", "samplelibrarysource",
    "samplelibraryselection",
})
# Titles and free text. Specific enough to recognise "single-nucleus 3' RNA-seq"
# in, but never enough on their own to conclude a field is "other" -- a sample
# title that says nothing about the library leaves the field unknown.
_CONTEXT_KEYS = frozenset({
    "experimenttitle", "title", "designdescription", "assay",
    "sampledescription", "sampletitle", "samplesourcenamech1",
})
_FILE_KEYS = frozenset({
    "fastqftp", "submittedftp", "srafttp", "sraftp", "fastqaspera",
    "submittedaspera", "filename", "filetype", "submittedformat", "fastqgalaxy",
})
# Byte counts in preference order. fastq and submitted are alternate
# representations of the same reads, so they are tried in turn, never summed.
# Bare ``size``/``bytes`` are SRA's spelling and are trusted only under a run or
# file container, since a sample attribute may legitimately be called "size".
_FASTQ_BYTE_KEYS = frozenset({"fastqbytes"})
_SUBMITTED_BYTE_KEYS = frozenset({"submittedbytes"})
_SCOPED_BYTE_KEYS = frozenset({"size", "bytes"})
_BYTE_CONTAINERS = frozenset({
    "run", "runset", "srafile", "srafiles", "file", "files", "alternatives",
})
_ACCESS_KEYS = frozenset({"dataaccess", "access", "accessstatus", "statustype"})
_PMID_KEYS = frozenset({
    "samplepubmedid", "seriespubmedid", "studypubmedid", "pubmedid", "pmid",
})

_CONTROLLED = re.compile(r"\b(?:controlled|restricted|dbgap|authorized|hra\d)")
_NUCLEI = re.compile(r"\b(?:single[\s-]*nucle(?:us|i)|nuclei|sn[\s-]?rna(?:[\s-]?seq)?)\b")
_TENX = re.compile(r"10x|chromium")
_SINGLE_CELL = re.compile(r"single[\s-]*cell|scrna|sc-rna|10x|chromium")
_THREE_PRIME = re.compile(r"3\s*(?:'|’|prime|p\b)")
_FIVE_PRIME = re.compile(r"5\s*(?:'|’|prime|p\b)")
_OTHER_ASSAY = re.compile(r"\b(?:smart[\s-]?seq\d*|full[\s-]length|bulk[\s-]rna|atac[\s-]seq)\b")
_EXPLICIT_CELL = re.compile(r"\b(?:single[\s-]cell|sc[\s-]?rna(?:[\s-]?seq)?)\b")


def _study_context(raw: Any) -> str:
    """Only study metadata linked to this sample can supplement library facts."""
    texts = _collect(raw, frozenset({"studytitle"}))
    if isinstance(raw, dict):
        sample_ids = _collect({"Sample_geo_accession": raw.get("Sample_geo_accession")},
                              frozenset({"samplegeoaccession"}))
        applicable = []
        for series in (raw.get("_series_context") or {}).values():
            members = _collect(series, frozenset({"seriessampleid"}))
            if set(sample_ids) & set(members):
                applicable.append((len(set(members)), series))
        # A SuperSeries may mix technologies. Prefer the smallest explicitly
        # linked cohort containing this sample (normally its SubSeries).
        if applicable:
            smallest = min(size for size, _ in applicable)
            for size, series in applicable:
                if size == smallest:
                    texts.extend(_collect(series, frozenset({"seriestitle", "seriessummary"})))
    return " ".join(texts).casefold()

# Raw records come from XML and JSON parsers, so nesting is shallow in practice;
# the cap turns a pathological document into a truncated scan rather than a
# RecursionError that fails the whole record.
_MAX_DEPTH = 40


def _key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def _walk(
    value: Any,
    wanted: frozenset[str],
    out: list[str],
    parent: str = "",
    depth: int = 0,
    scoped: bool = False,
) -> None:
    """Collect every scalar reachable under a key whose name is in ``wanted``.

    ``scoped`` additionally accepts SRA's bare ``size``/``bytes`` when they sit
    directly under a run or file container.
    """
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, dict):
        for name, nested in value.items():
            key = _key(name)
            if key in wanted or (
                scoped and key in _SCOPED_BYTE_KEYS and parent in _BYTE_CONTAINERS
            ):
                _flatten(nested, out, depth + 1)
            else:
                _walk(nested, wanted, out, key, depth + 1, scoped)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _walk(nested, wanted, out, parent, depth + 1, scoped)


def _flatten(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, dict):
        for nested in value.values():
            _flatten(nested, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _flatten(nested, out, depth + 1)
    elif value is not None and not isinstance(value, bool):
        text = str(value).strip()
        if text:
            out.append(text)


def _collect(
    raw: Any, wanted: frozenset[str], scoped: bool = False, parent: str = ""
) -> list[str]:
    out: list[str] = []
    _walk(raw, wanted, out, parent, scoped=scoped)
    return out


def _text_of(raw: Any, wanted: frozenset[str]) -> str:
    return " ".join(_collect(raw, wanted)).casefold()


def _sum_bytes(values: list[str]) -> int | None:
    """Total the byte counts ENA reports as ``;``-separated per-file lists."""
    total = 0
    seen = False
    for value in values:
        for part in re.split(r"[;,]", value):
            digits = part.strip()
            if digits.isdigit():
                total += int(digits)
                seen = True
    return total if seen else None


def _matching_run(raw: Any, accession: str | None) -> Any:
    """The RUN element for ``accession``.

    Sample fan-out copies the whole EXPERIMENT_PACKAGE onto every run it
    produces, so an unscoped size scan would charge each run with the byte total
    of every run in the package.
    """
    if not accession or not isinstance(raw, dict):
        return None
    runs = (raw.get("RUN_SET") or {}).get("RUN") if isinstance(raw.get("RUN_SET"), dict) else None
    if isinstance(runs, dict):
        runs = [runs]
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        found = run.get("@accession") or run.get("accession")
        if isinstance(found, str) and found.strip().upper() == accession:
            return run
    return None


def _run_bytes(raw: Any, accession: str | None) -> int | None:
    """Raw file size for this run, in preference order and never double-counted."""
    scope = _matching_run(raw, accession)
    # Selecting the RUN makes it the container, so its own bare ``size`` counts.
    parent = "run" if scope is not None else ""
    if scope is None:
        scope = raw
    for keys in (_FASTQ_BYTE_KEYS, _SUBMITTED_BYTE_KEYS):
        total = _sum_bytes(_collect(scope, keys))
        if total is not None:
            return total
    return _sum_bytes(_collect(scope, frozenset(), scoped=True, parent=parent))


class _RunFacts:
    """The run-level signals T0 derives from, whatever source produced them."""

    def __init__(self, raw: Any, source_ref: str, accession: str | None = None) -> None:
        self.source_ref = source_ref
        self.library = _text_of(raw, _LIBRARY_KEYS)
        self.context = _text_of(raw, _CONTEXT_KEYS)
        self.study_context = _study_context(raw)
        self.files = _text_of(raw, _FILE_KEYS)
        self.access = _text_of(raw, _ACCESS_KEYS)
        self.total_bytes = _run_bytes(raw, accession)

    @property
    def described(self) -> str:
        """Library evidence plus surrounding text, for recognising a value."""
        return f"{self.library} {self.context}"


def _derive_t0(field: str, facts: _RunFacts) -> Any | None:
    """Map run facts onto a tiered column, or ``None`` when nothing is known.

    ``other`` is a real answer rather than a shrug: the vocabularies reserve
    ``unknown`` for "no value found", so a library the source described but the
    vocabulary does not list resolves to ``other``. Recognising a specific value
    may draw on surrounding titles, but only library evidence licenses ``other``
    -- a sample title that never mentions the library leaves the field unknown.
    """
    described = facts.described

    if field == "Library":
        if _TENX.search(described):
            return "10x_Genomics"
        return "other" if facts.library else None

    if field == "Assay":
        # GEO's sample description may name the assay explicitly while a
        # shared extraction protocol mentions a different arm (review row 7).
        if _FIVE_PRIME.search(facts.context) and not _THREE_PRIME.search(facts.context):
            return "5_prime_gex"
        if _THREE_PRIME.search(facts.context) and not _FIVE_PRIME.search(facts.context):
            return "3_prime_gex"
        if _FIVE_PRIME.search(described) and not _THREE_PRIME.search(described):
            return "5_prime_gex"
        if _FIVE_PRIME.search(described) and _THREE_PRIME.search(described):
            return None
        if _THREE_PRIME.search(described):
            return "3_prime_gex"
        return "other" if _OTHER_ASSAY.search(described) else None

    if field == "RNASource":
        if _NUCLEI.search(described):
            return "single nuclei"
        if (_NUCLEI.search(facts.study_context) and not _EXPLICIT_CELL.search(facts.study_context)
                and not _EXPLICIT_CELL.search(facts.context)):
            return "single nuclei"
        if _SINGLE_CELL.search(described):
            return "single cell"
        return "other" if re.search(r"\bbulk\b", described) else None

    if field == "RawFile":
        if not facts.files:
            return None
        return "fastq" if re.search(r"fastq|\.fq\b", facts.files) else "other"

    if field == "RawSize_GB":
        if facts.total_bytes is None:
            return None
        return round(facts.total_bytes / 1_000_000_000, 4)

    if field == "License":
        if _CONTROLLED.search(f"{facts.access} {facts.files}"):
            return "controlled access"
        # Only a retrievable file listing evidences open access; silence is
        # unknown, not open.
        return "open access" if facts.files else None

    return None


def _derive_all(
    fields: list[str], facts: _RunFacts
) -> dict[str, tuple[Any, str | None]]:
    return {
        field: (value, facts.source_ref)
        for field in fields
        if (value := _derive_t0(field, facts)) is not None
    }


class TierExtractor:
    """Fills tiered columns the earlier extractors left empty.

    ``http_client`` is optional: without one, T0 is limited to metadata already
    in the raw record and T1 is skipped entirely, which keeps the cascade usable
    offline and in tests.
    """

    def __init__(
        self,
        config: dict[str, Any],
        schema: ProfileSchema | None = None,
        http_client: Any = None,
        source_cache: SourceCache | None = None,
    ) -> None:
        self._http = http_client
        self._source_cache = source_cache
        self._publication_cache: dict[str, dict[str, Any]] = {}
        self._publication_missing: list[str] = []
        self._review_policy = schema is not None and schema.review_policy
        self._columns = (
            {col.name: col for col in schema.columns} if schema is not None else {}
        )

        # The tier config owns the roster; intersecting with the schema keeps a
        # field the profile dropped from being acquired into a column that no
        # longer exists.
        roster = tiered_fields_from_config(config)
        if schema is not None:
            roster &= {
                col.name for col in schema.columns if col.acquisition == "tiered"
            }

        tiers = config.get("extraction", {}).get("tiers", {})
        self._enabled: list[tuple[str, tuple[str, ...]]] = []
        for tier in TIER_ORDER:
            tier_config = tiers.get(tier) or {}
            if not tier_config.get("enabled", False):
                continue
            fields = tuple(
                name for name in tier_config.get("fields", [])
                if isinstance(name, str) and name in roster
            )
            self._enabled.append((tier, fields))

    def acquire(
        self,
        record: dict[str, Any],
        raw_record: dict[str, Any],
        connector_id: str | None = None,
    ) -> dict[str, Any]:
        """Return ``record`` with tiered gaps filled and provenance attached.

        ``_field_acquisition`` is assigned, never extended: a completed pass is
        the record's whole tier provenance, matching the store's
        set-replacement semantics for the sidecar.
        """
        result = dict(record)
        rows: list[dict[str, Any]] = []

        for tier, fields in self._enabled:
            pending = [f for f in fields if is_fillable(result.get(f))]
            if tier == "T1" and self._review_policy:
                # Publication fields form one set: refreshing just a missing
                # DOI can otherwise leave an old PMID list or single year.
                pending = list(fields)
            if not pending:
                continue
            acquired = self._run_tier(tier, pending, raw_record, connector_id, result)
            if tier == "T1" and self._review_policy:
                for field in pending:
                    result[field] = None
            for field, (value, source_ref) in acquired.items():
                survivor = self._survives(field, value)
                if survivor is None:
                    continue
                result[field] = survivor
                rows.append({
                    "field_name": field,
                    "tier": tier,
                    "source_ref": source_ref,
                })
            if tier == "T1" and self._review_policy and self._publication_missing:
                from metadata_agent.store.canonical import PENDING_PUBLICATIONS

                result["_status"] = {"reason": PENDING_PUBLICATIONS}
                result.setdefault("_diagnostics", []).append({
                    "column": "Pubmed ID", "type": "publication_fetch_incomplete",
                    "value": ", ".join(self._publication_missing),
                })

        result["_field_acquisition"] = rows
        return result

    def _run_tier(
        self,
        tier: str,
        fields: list[str],
        raw_record: dict[str, Any],
        connector_id: str | None,
        record: dict[str, Any],
    ) -> dict[str, tuple[Any, str | None]]:
        if tier == "T0":
            return self._tier0(fields, raw_record, connector_id)
        if tier == "T1":
            return self._tier1(fields, raw_record, connector_id, record)
        # Unreachable: load_config refuses to build a config with T2/T3 enabled.
        raise ValueError(f"extraction.tiers.{tier} is enabled but not implemented")

    def _survives(self, field: str, value: Any) -> Any | None:
        """The value as the column would store it, or ``None`` if it would not."""
        if is_fillable(value):
            return None
        column = self._columns.get(field)
        if column is None or not column.number or column.numeric_kind is None:
            return value
        coerced, _ = coerce_number(
            value, field, column.numeric_kind, bounds_for(field)
        )
        return coerced

    def _tier0(
        self,
        fields: list[str],
        raw_record: dict[str, Any],
        connector_id: str | None,
    ) -> dict[str, tuple[Any, str | None]]:
        raw = raw_record.get("raw", raw_record)
        accession = _first_match(raw_record, _RUN_ACCESSION)
        local = _RunFacts(raw, f"{connector_id or 'raw'}:{accession or 'record'}", accession)
        found = _derive_all(fields, local)

        # Ask ENA only for what the fetched record could not answer, and merge:
        # a record with library text but no file listing still needs the report
        # for RawFile and RawSize_GB.
        missing = [field for field in fields if field not in found]
        if missing and accession:
            remote = self._filereport_facts(accession, accession)
            if remote is not None:
                found.update(_derive_all(missing, remote))
        return found

    def _filereport_facts(
        self, accession: str, run_accession: str | None = None
    ) -> _RunFacts | None:
        if self._http is None:
            return None
        params = {
            "accession": accession,
            "result": "read_run",
            "fields": ",".join(_FILEREPORT_FIELDS),
            "format": "tsv",
        }
        if self._source_cache is not None:
            rows = self._source_cache.get_or_load(
                "ena_filereport_v1", params, lambda: self._filereport_rows(params),
                valid=lambda value: isinstance(value, list) and bool(value)
                and all(isinstance(row, dict) and (row.get("run_accession") or row.get("experiment_accession"))
                        for row in value),
            )
        else:
            rows = self._filereport_rows(params)
        # A study or sample accession reports every run under it; keep only this
        # record's own run so its size is not charged the whole study's total.
        if run_accession:
            own = [
                row for row in rows
                if str(row.get("run_accession", "")).strip().upper() == run_accession
            ]
            rows = own or rows
        if not rows:
            return None
        return _RunFacts(rows, f"{ENA_FILEREPORT_URL}?accession={accession}")

    def _filereport_rows(self, params: dict[str, str]) -> list[dict[str, Any]]:
        with timed("ena"):
            response = self._http.get(ENA_FILEREPORT_URL, params=params)
        if not response.ok:
            logger.warning(
                "T0 filereport failed for %s: %s", params["accession"], response.error
            )
            return []

        return [
            row for row in csv.DictReader(
                io.StringIO(response.text or ""), delimiter="\t"
            )
            if any(v for v in row.values() if v)
        ]

    def _tier1(
        self,
        fields: list[str],
        raw_record: dict[str, Any],
        connector_id: str | None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, tuple[Any, str | None]]:
        if self._review_policy:
            return self._review_publications(fields, raw_record, connector_id, record)
        # An existing Pubmed ID wins: resolving a second one would describe the
        # publication fields from a different article than the stored PMID.
        existing = (record or {}).get("Pubmed ID")
        pmid = (
            str(existing).strip() if not is_fillable(existing)
            else self._resolve_pmid(raw_record, connector_id)
        )
        if not pmid:
            return {}

        source_ref = f"{PUBMED_URL}/{pmid}/"
        found: dict[str, tuple[Any, str | None]] = {"Pubmed ID": (pmid, source_ref)}

        document = self._pubmed_summary(pmid)
        if document:
            doi = _doi_of(document)
            journal = _first_text(document, "fulljournalname", "source")
            year = _year_of(document)
            if doi:
                found["Citation"] = (doi, source_ref)
            if journal:
                found["Journal"] = (journal, source_ref)
            if year:
                found["Year"] = (year, source_ref)

        return {f: v for f, v in found.items() if f in fields}

    def _review_publications(
        self, fields: list[str], raw_record: dict[str, Any], connector_id: str | None,
        record: dict[str, Any] | None,
    ) -> dict[str, tuple[Any, str | None]]:
        existing = (record or {}).get("Pubmed ID")
        self._publication_missing = []
        raw_ids = _collect(raw_record.get("raw", raw_record), _PMID_KEYS)
        if not is_fillable(existing):
            raw_ids = [str(existing), *raw_ids]
        pmids = list(dict.fromkeys(
            part for value in raw_ids for part in re.split(r"[,;\s]+", value.strip())
            if part.isdigit() and int(part) > 0
        ))
        if not pmids:
            linked = self._elink_pmid(raw_record, connector_id)
            pmids = linked.split(", ") if linked else []
        publications = []
        for pmid in pmids:
            doc = self._pubmed_summary(pmid)
            doi = _doi_of(doc)
            journal = _first_text(doc, "fulljournalname", "source")
            # A bare/unresolved PMID must not produce a fabricated citation.
            if doi or journal:
                publications.append((pmid, doi, journal, _year_of(doc)))
            else:
                self._publication_missing.append(pmid)
        if not publications:
            return {}
        source_ref = ", ".join(f"{PUBMED_URL}/{p[0]}/" for p in publications)
        found = {}
        for index, field in enumerate(("Pubmed ID", "Citation", "Journal", "Year")):
            values = list(dict.fromkeys(p[index] for p in publications if p[index]))
            if not values or (field == "Year" and (len(values) != 1 or self._publication_missing)):
                continue
            found[field] = (", ".join(values), source_ref)
        return {f: v for f, v in found.items() if f in fields}

    def _resolve_pmid(
        self, raw_record: dict[str, Any], connector_id: str | None
    ) -> str | None:
        for value in _collect(raw_record.get("raw", raw_record), _PMID_KEYS):
            digits = re.search(r"\d+", value)
            if digits:
                return digits.group(0)
        return self._elink_pmid(raw_record, connector_id)

    def _elink_pmid(
        self, raw_record: dict[str, Any], connector_id: str | None
    ) -> str | None:
        """Ask eLink which publication cites the record's study."""
        if self._http is None:
            return None

        geo = _first_match(raw_record, _GEO_ACCESSION)
        accession, db = (geo, "gds") if geo else (
            _first_match(raw_record, _RUN_ACCESSION), "sra"
        )
        if accession is None:
            return None

        # eLink joins internal UIDs, not accessions, so the accession has to be
        # resolved through esearch the way the SRA connector already does it.
        uid = self._esearch_uid(db, accession)
        if uid is None:
            return None

        if self._source_cache is not None:
            return self._source_cache.get_or_load(
                "pubmed_links_v1", (db, uid, self._review_policy),
                lambda: self._elink_ids(db, uid, accession),
                valid=lambda value: isinstance(value, str) and bool(value)
                and all(part.strip().isdigit() for part in value.split(",")),
            )
        return self._elink_ids(db, uid, accession)

    def _elink_ids(self, db: str, uid: str, accession: str) -> str | None:
        with timed("pubmed"):
            response = self._http.get(ELINK_URL, params={
                "dbfrom": db, "db": "pubmed", "id": uid, "retmode": "json",
            })
        if not response.ok:
            logger.warning("T1 eLink failed for %s: %s", accession, response.error)
            return None

        pmids = []
        for linkset in _json_path(response.text, "linksets") or []:
            for linked in (linkset or {}).get("linksetdbs") or []:
                if (linked or {}).get("dbto") != "pubmed":
                    continue
                for link in linked.get("links") or []:
                    if str(link).strip().isdigit():
                        pmids.append(str(link).strip())
        if self._review_policy:
            return ", ".join(dict.fromkeys(pmids)) or None
        return pmids[0] if pmids else None

    def _esearch_uid(self, db: str, accession: str) -> str | None:
        if self._source_cache is not None:
            return self._source_cache.get_or_load(
                "ncbi_uid_v1", (db, accession), lambda: self._esearch_uid_uncached(db, accession),
                valid=lambda value: isinstance(value, str) and value.isdigit(),
            )
        return self._esearch_uid_uncached(db, accession)

    def _esearch_uid_uncached(self, db: str, accession: str) -> str | None:
        with timed("pubmed"):
            response = self._http.get(ESEARCH_URL, params={
                "db": db, "term": f"{accession}[accn]", "retmax": 1, "retmode": "json",
            })
        if not response.ok:
            logger.warning("T1 esearch failed for %s: %s", accession, response.error)
            return None
        result = _json_path(response.text, "esearchresult") or {}
        ids = result.get("idlist") or [] if isinstance(result, dict) else []
        return str(ids[0]) if ids else None

    def _pubmed_summary(self, pmid: str) -> dict[str, Any]:
        if self._source_cache is not None:
            return self._source_cache.get_or_load(
                "pubmed_summary_v1", pmid, lambda: self._pubmed_summary_uncached(pmid),
                valid=lambda value: isinstance(value, dict) and not value.get("error")
                and bool(_doi_of(value) or _first_text(value, "fulljournalname", "source")),
            )
        if pmid in self._publication_cache:
            count("source_cache_hits")
            return self._publication_cache[pmid]
        document = self._pubmed_summary_uncached(pmid)
        if _doi_of(document) or _first_text(document, "fulljournalname", "source"):
            self._publication_cache[pmid] = document
        return document

    def _pubmed_summary_uncached(self, pmid: str) -> dict[str, Any]:
        if self._http is None:
            return {}
        with timed("pubmed"):
            response = self._http.get(ESUMMARY_URL, params={
                "db": "pubmed", "id": pmid, "retmode": "json",
            })
        if not response.ok:
            logger.warning("T1 esummary failed for %s: %s", pmid, response.error)
            return {}
        result = _json_path(response.text, "result") or {}
        document = result.get(pmid) if isinstance(result, dict) else None
        if isinstance(document, dict):
            return document
        return {}


def _json_path(text: str | None, key: str) -> Any:
    try:
        payload = json.loads(text or "{}")
    except (ValueError, TypeError):
        return None
    return payload.get(key) if isinstance(payload, dict) else None


def _first_match(raw_record: dict[str, Any], pattern: re.Pattern[str]) -> str | None:
    """The record's own accession when it fits, else the first one in its body."""
    accession = raw_record.get("accession")
    if isinstance(accession, str) and pattern.fullmatch(accession.strip()):
        return accession.strip().upper()
    out: list[str] = []
    _flatten(raw_record.get("raw", raw_record), out)
    for value in out:
        match = pattern.search(value)
        if match:
            return match.group(0).upper()
    return None


def _first_text(document: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _doi_of(document: dict[str, Any]) -> str | None:
    """The article's DOI -- the schema defines Citation as a DOI, nothing else."""
    article_ids = document.get("articleids")
    if isinstance(article_ids, dict):
        article_ids = [article_ids]
    for entry in article_ids or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("idtype", "")).casefold() == "doi":
            value = entry.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()

    elocation = _first_text(document, "elocationid")
    match = _DOI.search(elocation) if elocation else None
    return match.group(0).rstrip(".,;") if match else None


def _year_of(document: dict[str, Any]) -> str | None:
    for key in ("sortpubdate", "pubdate", "epubdate"):
        value = _first_text(document, key)
        match = _YEAR.search(value) if value else None
        if match:
            return match.group(0)
    return None
