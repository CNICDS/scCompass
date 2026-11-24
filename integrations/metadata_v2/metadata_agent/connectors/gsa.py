from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any, Iterator

from metadata_agent.connectors.base import SourceConnector, register_connector
from metadata_agent.connectors.ngdc_biosample import BIOSAMPLE_BROWSE_URL, parse_samc_page
from metadata_agent.connectors.query_builder import resolve_query
from metadata_agent.connectors.xml_helpers import normalize_raw

logger = logging.getLogger(__name__)

GSA_BROWSE_URL = "https://ngdc.cncb.ac.cn/gsa/browse"
GSA_RUN_INFO_URL = "https://ngdc.cncb.ac.cn/gsa/getRunInfo"

_CRA_RE = re.compile(r"(CRA\d+)")
_GSA_XREF_RE = re.compile(r"\b(CR[RA]\d{6,10}|SAM[NEDC][A-Z]?\d{6,10})\b")
_DEFAULT_PAGE_SIZE = 25
_DEFAULT_MAX_PAGES = 400


def _extract_gsa_xrefs(text: str) -> list[str]:
    """Deduplicated CRA/CRR study/run and external BioSample accessions on a page."""
    out: list[str] = []
    for token in _GSA_XREF_RE.findall(text or ""):
        if token not in out:
            out.append(token)
    return out


@register_connector
class GSAConnector(SourceConnector):
    name = "gsa"

    def search(self, since: str | None = None, until: str | None = None) -> Iterator[str]:
        """Browse GSA studies for the configured keyword query.

        GSA has no official API and no date filter, so this walks the browse
        listing's real query parameters (not an accession in the URL path),
        follows pagination, deduplicates CRA IDs, and logs parse/count anomalies
        rather than silently yielding nothing on markup drift.
        """
        query = resolve_query("gsa", self.config)
        if since or until:
            logger.info("GSA has no date filter; ignoring since=%s until=%s", since, until)

        page_size = int(self.config.get("browse_page_size", _DEFAULT_PAGE_SIZE))
        max_pages = int(self.config.get("browse_max_pages", _DEFAULT_MAX_PAGES))

        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            params = {"searchTerm": query, "pageSize": page_size, "page": page}
            result = self.http.get(GSA_BROWSE_URL, params=params)
            if not result.ok:
                logger.warning("GSA browse page %d failed: %s", page, result.error)
                break

            text = result.text or ""
            page_cras = self._parse_cra(text)
            if not page_cras:
                if self._looks_like_listing(text):
                    logger.info("GSA browse page %d: end of listing (%d CRA total)", page, len(seen))
                else:
                    logger.warning(
                        "GSA browse page %d: no CRA and unrecognized markup — "
                        "possible drift, stopping with %d CRA collected", page, len(seen),
                    )
                break

            new = [c for c in page_cras if c not in seen]
            if not new:
                logger.info("GSA browse page %d returned only seen CRA; stopping", page)
                break
            for cra in new:
                seen.add(cra)
                yield cra
        else:
            logger.warning("GSA browse hit page cap %d; results may be bounded", max_pages)

        logger.info("GSA browse query=%r yielded %d unique CRA", query, len(seen))

    @staticmethod
    def _parse_cra(text: str) -> list[str]:
        out: list[str] = []
        for cra in _CRA_RE.findall(text):
            if cra not in out:
                out.append(cra)
        return out

    @staticmethod
    def _looks_like_listing(text: str) -> bool:
        low = text.lower()
        return "<table" in low or "browse" in low or "gsa" in low

    def fetch(self, accession: str) -> dict[str, Any]:
        accession = accession.strip().upper()
        if accession.startswith("SAMC"):
            return self._fetch_samc(accession)
        run_info = self._fetch_run_info(accession)
        browse_data = self._fetch_browse_page(accession)

        raw: dict[str, Any] = {
            "run_info": run_info,
            "browse": browse_data,
        }
        return normalize_raw(
            raw, "gsa",
            f"{GSA_BROWSE_URL}/{accession}", accession,
        )

    def fetch_by_accession(self, acc: str) -> dict[str, Any]:
        """Fetch a GSA accession. ``SAMC`` (NGDC BioSample) is resolved to its
        detail page, from which the CRA/CRR study/run and external BioSample
        siblings (``SAM*``) are extracted as ``_links`` for the resolver; CRA/CRR
        accessions use the standard run-info + browse fetch.
        """
        return self.fetch(acc)

    def _fetch_samc(self, acc: str) -> dict[str, Any]:
        url = f"{BIOSAMPLE_BROWSE_URL}/{acc}"
        result = self.http.get(url)
        if not result.ok:
            return normalize_raw({"error": result.error or "fetch failed"}, "gsa", url, acc)

        raw = parse_samc_page(result.text or "", acc)
        if raw.get("error"):
            logger.warning("NGDC BioSample %s: %s", acc, raw["error"])
        return normalize_raw(raw, "gsa", url, acc)

    def _fetch_run_info(self, accession: str) -> list[dict[str, str]] | dict[str, str]:
        params = {"CRAFullAccession": accession}
        result = self.http.get(GSA_RUN_INFO_URL, params=params)
        if not result.ok:
            return {"error": result.error or "fetch failed"}

        text = result.text or ""
        content_type = result.headers.get("Content-Type", "")

        if "text/html" in content_type or text.strip().startswith("<!") or text.strip().startswith("<html"):
            logger.warning("GSA getRunInfo returned HTML for %s — marking failed", accession)
            return {"error": "HTML response instead of CSV"}

        try:
            reader = csv.DictReader(io.StringIO(text))
            return [dict(row) for row in reader]
        except Exception as exc:
            logger.warning("GSA CSV parse failed for %s: %s", accession, exc)
            return {"error": f"CSV parse error: {exc}"}

    def _fetch_browse_page(self, accession: str) -> dict[str, Any]:
        url = f"{GSA_BROWSE_URL}/{accession}"
        result = self.http.get(url)
        if not result.ok:
            return {"error": result.error}

        text = result.text or ""
        data: dict[str, Any] = {"raw_html_length": len(text)}

        title_match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE)
        if title_match:
            data["title"] = title_match.group(1)

        sample_ids = re.findall(r"(SAMC\d+)", text)
        if sample_ids:
            # Sorted, not set order: this feeds content_hash, which must be
            # stable across processes or every re-harvest looks like a change.
            data["sample_ids"] = sorted(set(sample_ids))

        return data

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw
