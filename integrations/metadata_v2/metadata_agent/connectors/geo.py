from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from metadata_agent.connectors.base import SourceConnector, register_connector
from metadata_agent.connectors.ncbi_paging import esearch_all
from metadata_agent.connectors.query_builder import resolve_query
from metadata_agent.connectors.xml_helpers import normalize_raw

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
GEO_ACC_URL = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"

_ESUMMARY_BATCH = 500


@register_connector
class GEOConnector(SourceConnector):
    name = "geo"

    def search(self, since: str | None = None, until: str | None = None) -> Iterator[str]:
        """Discover GSM samples directly.

        The query is constrained to GSM entry type (``gsm[ETYP]``, added by the
        query builder), so series lacking a curated GDS are no longer under-
        sampled. Full history paging comes from ``esearch_all``; the returned
        GDS UIDs are translated to ``GSM`` accessions via batched ``esummary``.
        """
        base: dict[str, Any] = {
            "db": "gds",
            "term": resolve_query("geo", self.config),
        }
        date_field = self.config.get("date_field", "MDAT")
        if since or until:
            base["datetype"] = date_field
            if since:
                base["mindate"] = since
            if until:
                base["maxdate"] = until

        yield from self._uids_to_gsm(esearch_all(self.http, base, label="geo"))

    def _uids_to_gsm(self, uids: Iterator[str]) -> Iterator[str]:
        batch: list[str] = []
        for uid in uids:
            batch.append(uid)
            if len(batch) >= _ESUMMARY_BATCH:
                yield from self._esummary_gsm(batch)
                batch = []
        if batch:
            yield from self._esummary_gsm(batch)

    def _esummary_gsm(self, uids: list[str]) -> Iterator[str]:
        params = {"db": "gds", "id": ",".join(uids), "retmode": "json"}
        result = self.http.get(ESUMMARY_URL, params=params)
        if not result.ok:
            logger.warning("GEO esummary failed for %d UIDs: %s", len(uids), result.error)
            return
        try:
            payload = json.loads(result.text or "{}").get("result", {})
        except (ValueError, TypeError):
            logger.warning("GEO esummary returned unparseable JSON for %d UIDs", len(uids))
            return
        for uid in payload.get("uids", []):
            doc = payload.get(uid, {})
            accession = doc.get("accession", "") if isinstance(doc, dict) else ""
            if accession.startswith("GSM"):
                yield accession
            elif accession:
                logger.debug("GEO esummary UID %s resolved to non-GSM %s; skipped", uid, accession)

    def fetch(self, accession: str) -> dict[str, Any]:
        params = {
            "acc": accession,
            "targ": "self",
            "form": "text",
            "view": "quick",
        }
        result = self.http.get(GEO_ACC_URL, params=params)
        if not result.ok:
            return normalize_raw(
                {"error": result.error}, "geo",
                f"{GEO_ACC_URL}?acc={accession}", accession,
            )

        parsed = self._parse_soft(result.text or "")
        # Fetch every linked series once per connector. Keep project context
        # separate so cohort descriptions cannot masquerade as sample traits.
        if self.config.get("fetch_series_context", True):
            cache = getattr(self, "_series_cache", {})
            self._series_cache = cache
            contexts = {}
            for series in parsed.get("Sample_series_id", []):
                if not re.fullmatch(r"GSE\d+", series):
                    continue
                if series not in cache:
                    response = self.http.get(GEO_ACC_URL, params={
                        "acc": series, "targ": "self", "form": "text", "view": "quick",
                    })
                    if response.ok:
                        context = self._parse_soft(response.text or "")
                        context = {k: v for k, v in context.items() if k.startswith("Series_")}
                        if context:
                            cache[series] = context
                if series in cache:
                    contexts[series] = cache[series]
            if contexts:
                parsed["_series_context"] = contexts
            if any(series not in contexts for series in parsed.get("Sample_series_id", [])):
                parsed["_series_context_incomplete"] = True
        return normalize_raw(
            parsed, "geo",
            f"{GEO_ACC_URL}?acc={accession}", accession,
        )

    def _parse_soft(self, text: str) -> dict[str, Any]:
        data: dict[str, Any] = {}
        characteristics: list[str] = []
        relations: list[str] = []

        for line in text.splitlines():
            if line.startswith("!"):
                line = line[1:]
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip().lstrip("^")
                    val = val.strip()
                    if key == "Sample_characteristics_ch1":
                        characteristics.append(val)
                    elif key == "Sample_relation":
                        relations.append(val)
                    elif key == "Sample_series_id":
                        data.setdefault("Sample_series_id", []).append(val)
                    else:
                        if key not in data:
                            data[key] = val
                        elif isinstance(data[key], list):
                            data[key].append(val)
                        else:
                            data[key] = [data[key], val]

        if characteristics:
            data["Sample_characteristics_ch1"] = characteristics
        if relations:
            data["Sample_relation"] = relations
        return data

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw
