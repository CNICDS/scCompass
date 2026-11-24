from __future__ import annotations

import logging
from typing import Any, Iterator

from metadata_agent.connectors.base import SourceConnector, register_connector
from metadata_agent.connectors.ncbi_paging import esearch_all
from metadata_agent.connectors.query_builder import resolve_query
from metadata_agent.connectors.xml_helpers import (
    normalize_raw,
    run_accessions_from_run_set,
    xml_to_dict,
)

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
ELINK_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"


@register_connector
class SRAConnector(SourceConnector):
    name = "sra"

    def search(self, since: str | None = None, until: str | None = None) -> Iterator[str]:
        base: dict[str, Any] = {
            "db": "sra",
            "term": resolve_query("sra", self.config),
        }
        date_field = self.config.get("date_field", "PDAT")
        if since or until:
            base["datetype"] = date_field
            if since:
                base["mindate"] = since
            if until:
                base["maxdate"] = until
        yield from esearch_all(self.http, base, label="sra")

    def fetch(self, accession: str) -> dict[str, Any]:
        params = {"db": "sra", "id": accession, "retmode": "xml"}
        result = self.http.get(EFETCH_URL, params=params)
        if not result.ok:
            return normalize_raw(
                {"error": result.error}, "sra",
                f"{EFETCH_URL}?db=sra&id={accession}", accession,
            )

        parsed = xml_to_dict(result.text or "<empty/>")
        attributes = self._extract_attributes(parsed)
        parsed["_attributes"] = attributes

        gsm_link = self._link_to_gsm(accession)
        if gsm_link:
            parsed["_gsm_link"] = gsm_link

        return normalize_raw(
            parsed, "sra",
            f"{EFETCH_URL}?db=sra&id={accession}", accession,
        )

    def fetch_by_accession(self, acc: str) -> dict[str, Any]:
        """Fetch a single run/experiment/sample accession (SRR/ERR/DRR/SRX...).

        NCBI efetch accepts these accessions directly as ``id``. If efetch
        returns nothing usable, fall back to esearch by ``[accn]`` and refetch
        via the resolved internal UID.
        """
        parsed = self._efetch_xml(acc)
        if parsed is not None and parsed.get("EXPERIMENT_PACKAGE_SET"):
            attributes = self._extract_attributes(parsed)
            parsed["_attributes"] = attributes
            return normalize_raw(
                parsed, "sra", f"{EFETCH_URL}?db=sra&id={acc}", acc,
            )

        uid = self._resolve_accession(acc)
        if uid is not None:
            parsed = self._efetch_xml(uid)
            if parsed is not None:
                attributes = self._extract_attributes(parsed)
                parsed["_attributes"] = attributes
                return normalize_raw(
                    parsed, "sra", f"{EFETCH_URL}?db=sra&id={uid}", acc,
                )

        return normalize_raw(
            {"error": f"no SRA record for {acc}"}, "sra",
            f"{EFETCH_URL}?db=sra&id={acc}", acc,
        )

    def fetch_batch(self, accessions: list[str]) -> list[dict[str, Any]]:
        """Batch-fetch run-level accessions via efetch, chunked at 50 IDs.

        Parses ``EXPERIMENT_PACKAGE_SET`` and emits one normalized raw record
        per run accession found. Chunks that fail are skipped (partial success).
        """
        results: list[dict[str, Any]] = []
        for start in range(0, len(accessions), 50):
            chunk = accessions[start:start + 50]
            parsed = self._efetch_xml(",".join(chunk))
            if parsed is None:
                logger.error("SRA batch efetch failed for chunk %r", chunk)
                continue
            results.extend(self._split_packages(parsed))
        return results

    def _efetch_xml(self, ids: str) -> dict[str, Any] | None:
        params = {"db": "sra", "id": ids, "retmode": "xml"}
        result = self.http.get(EFETCH_URL, params=params)
        if not result.ok:
            return None
        return xml_to_dict(result.text or "<empty/>")

    def _resolve_accession(self, acc: str) -> str | None:
        params = {
            "db": "sra", "term": f"{acc}[accn]",
            "retmax": 1, "retmode": "json",
        }
        result = self.http.get(ESEARCH_URL, params=params)
        if not result.ok:
            return None
        import json
        try:
            data = json.loads(result.text)
            ids = data.get("esearchresult", {}).get("idlist", [])
            return ids[0] if ids else None
        except (ValueError, KeyError, IndexError):
            return None

    def _split_packages(self, parsed: dict[str, Any]) -> list[dict[str, Any]]:
        pkg_set = parsed.get("EXPERIMENT_PACKAGE_SET", {}) or {}
        packages = pkg_set.get("EXPERIMENT_PACKAGE", [])
        if isinstance(packages, dict):
            packages = [packages]

        records: list[dict[str, Any]] = []
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            attributes = self._extract_attributes({
                "EXPERIMENT_PACKAGE_SET": {"EXPERIMENT_PACKAGE": pkg}
            })
            run_accs = run_accessions_from_run_set(pkg.get("RUN_SET", {}))
            if not run_accs:
                exp = pkg.get("EXPERIMENT", {})
                acc = exp.get("@accession") if isinstance(exp, dict) else None
                run_accs = [acc] if acc else [""]
            for run_acc in run_accs:
                record = dict(pkg)
                record["_attributes"] = attributes
                records.append(normalize_raw(
                    record, "sra",
                    f"{EFETCH_URL}?db=sra&id={run_acc}", run_acc,
                ))
        return records

    def _extract_attributes(self, parsed: dict[str, Any]) -> dict[str, str]:
        attrs: dict[str, str] = {}
        try:
            pkg = parsed.get("EXPERIMENT_PACKAGE_SET", {}).get("EXPERIMENT_PACKAGE", {})
            if isinstance(pkg, list):
                pkg = pkg[0]
            sample = pkg.get("SAMPLE", {})
            sample_attrs = sample.get("SAMPLE_ATTRIBUTES", {}).get("SAMPLE_ATTRIBUTE", [])
            if isinstance(sample_attrs, dict):
                sample_attrs = [sample_attrs]
            for attr in sample_attrs:
                tag = attr.get("TAG", "")
                value = attr.get("VALUE", "")
                if tag:
                    attrs[tag] = value
        except (AttributeError, TypeError, KeyError):
            pass
        return attrs

    def _link_to_gsm(self, sra_id: str) -> str | None:
        """Resolve an SRA id to its linked GEO ``GSM`` accession.

        ``elink`` returns a GDS **UID** (numeric), not a GSM accession, so the
        UID is resolved through ``esummary`` before it can be routed.
        """
        uid = self._elink_gds_uid(sra_id)
        if uid is None:
            return None
        return self._gds_uid_to_gsm(uid)

    def _elink_gds_uid(self, sra_id: str) -> str | None:
        params = {"dbfrom": "sra", "db": "gds", "id": sra_id, "retmode": "json"}
        result = self.http.get(ELINK_URL, params=params)
        if not result.ok:
            return None
        import json
        try:
            linksets = json.loads(result.text).get("linksets", [])
            for ls in (linksets[0].get("linksetdbs", []) if linksets else []):
                if ls.get("dbto") == "gds":
                    ids = ls.get("links", [])
                    if ids:
                        return ids[0]
        except (ValueError, KeyError, IndexError):
            pass
        return None

    def _gds_uid_to_gsm(self, uid: str) -> str | None:
        params = {"db": "gds", "id": uid, "retmode": "json"}
        result = self.http.get(ESUMMARY_URL, params=params)
        if not result.ok:
            return None
        import json
        try:
            payload = json.loads(result.text).get("result", {})
            doc = payload.get(str(uid), {})
            accession = doc.get("accession", "") if isinstance(doc, dict) else ""
            if isinstance(accession, str) and accession.startswith("GSM"):
                return accession
        except (ValueError, KeyError, TypeError):
            pass
        return None

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw
