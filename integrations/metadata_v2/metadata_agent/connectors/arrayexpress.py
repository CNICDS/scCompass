from __future__ import annotations

import csv
import io
import logging
from typing import Any, Iterator

from metadata_agent.connectors.base import SourceConnector, register_connector
from metadata_agent.connectors.query_builder import resolve_query
from metadata_agent.connectors.xml_helpers import normalize_raw

logger = logging.getLogger(__name__)

BIOSTUDIES_SEARCH_URL = "https://www.ebi.ac.uk/biostudies/api/v1/arrayexpress/search"
BIOSTUDIES_STUDY_URL = "https://www.ebi.ac.uk/biostudies/api/v1/studies"
BIOSTUDIES_FILES_URL = "https://www.ebi.ac.uk/biostudies/files"


@register_connector
class ArrayExpressConnector(SourceConnector):
    name = "arrayexpress"

    def search(self, since: str | None = None, until: str | None = None) -> Iterator[str]:
        query = resolve_query("arrayexpress", self.config)
        params: dict[str, Any] = {
            "query": query,
            "pageSize": 100,
            "page": 1,
        }
        if since:
            params["modifiedSince"] = since
        if until:
            params["modifiedUntil"] = until

        seen: set[str] = set()
        while True:
            result = self.http.get(BIOSTUDIES_SEARCH_URL, params=params)
            if not result.ok:
                logger.error("ArrayExpress search failed: %s", result.error)
                break

            import json
            data = json.loads(result.text)
            hits = data.get("hits", [])
            if not hits:
                break

            for hit in hits:
                acc = hit.get("accession", "")
                if acc and acc not in seen:
                    seen.add(acc)
                    yield acc

            total_pages = data.get("totalPages", 1)
            if params["page"] >= total_pages:
                break
            params["page"] += 1

        logger.info("ArrayExpress query=%r yielded %d unique accessions", query, len(seen))

    def fetch(self, accession: str) -> dict[str, Any]:
        sdrf_data = self._fetch_sdrf(accession)
        study_data = self._fetch_study(accession)

        raw = {
            "study": study_data,
            "sdrf_samples": sdrf_data,
        }
        return normalize_raw(
            raw, "arrayexpress",
            f"{BIOSTUDIES_STUDY_URL}/{accession}", accession,
        )

    def _fetch_study(self, accession: str) -> dict[str, Any]:
        url = f"{BIOSTUDIES_STUDY_URL}/{accession}"
        result = self.http.get(url)
        if not result.ok:
            return {"error": result.error}
        import json
        return json.loads(result.text)

    def _fetch_sdrf(self, accession: str) -> list[dict[str, str]]:
        url = f"{BIOSTUDIES_FILES_URL}/{accession}/{accession}.sdrf.txt"
        result = self.http.get(url)
        if not result.ok:
            alt_url = f"{BIOSTUDIES_FILES_URL}/{accession}/{accession}.sdrf.tsv"
            result = self.http.get(alt_url)
            if not result.ok:
                logger.warning("SDRF not found for %s", accession)
                return []

        return self._parse_sdrf(result.text or "")

    def _parse_sdrf(self, text: str) -> list[dict[str, str]]:
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        samples: list[dict[str, str]] = []
        for row in reader:
            samples.append(dict(row))
        return samples

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw
