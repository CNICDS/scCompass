"""BioSample connector — the INSDC cross-link hub.

BioSample ties GEO, SRA and ENA records together: a GSM, an SRS/SRX and an
ERS/ERX describing the same biological sample all point at one ``SAM*``
accession. This connector fetches that hub record so the cross-link resolver can
union sibling attributes into a sample's canonical row.

Providers by prefix (see ``accession_router``): ``SAMN`` → NCBI
(``efetch db=biosample``), ``SAMEA``/``SAMD`` → ENA Browser XML. Each path falls
back to the other provider when the first has no record. ``SAMC`` (NGDC) is
routed to the GSA connector, not here.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator

from metadata_agent.connectors.base import SourceConnector, register_connector
from metadata_agent.connectors.ncbi_paging import esearch_all
from metadata_agent.connectors.query_builder import resolve_query
from metadata_agent.connectors.xml_helpers import normalize_raw, xml_to_dict

logger = logging.getLogger(__name__)

NCBI_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ENA_BROWSER_URL = "https://www.ebi.ac.uk/ena/browser/api/xml"

# Cross-reference accessions worth following from a BioSample hub record.
_XREF_RE = re.compile(
    r"\b([SED]R[RXSP]\d{6,10}|SAM[NEDC][A-Z]?\d{6,10}|GS[EM]\d{4,10}|CR[RA]\d{6,10})\b"
)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _norm_key(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


@register_connector
class BioSampleConnector(SourceConnector):
    name = "biosample"

    def search(self, since: str | None = None, until: str | None = None) -> Iterator[str]:
        """Discover BioSample UIDs (NCBI ``db=biosample``).

        Implemented for completeness; BioSample is used primarily as an
        enrichment hub and is disabled for discovery by default.
        """
        base: dict[str, Any] = {
            "db": "biosample",
            "term": resolve_query("biosample", self.config),
        }
        date_field = self.config.get("date_field", "MDAT")
        if since or until:
            base["datetype"] = date_field
            if since:
                base["mindate"] = since
            if until:
                base["maxdate"] = until
        yield from esearch_all(self.http, base, label="biosample")

    def fetch(self, accession: str) -> dict[str, Any]:
        return self.fetch_by_accession(accession)

    def fetch_by_accession(self, acc: str) -> dict[str, Any]:
        """Fetch a ``SAM*`` record, preferring its native provider.

        ``SAMN`` → NCBI, ``SAMEA``/``SAMD`` → ENA; either falls back to the other
        provider when the first returns no usable record.
        """
        prefix = acc.strip().upper()
        ncbi_first = prefix.startswith("SAMN")

        order = (self._fetch_ncbi, self._fetch_ena) if ncbi_first else (self._fetch_ena, self._fetch_ncbi)
        for fetcher in order:
            parsed = fetcher(acc)
            if parsed is not None:
                url = parsed.pop("_source_url", "")
                return normalize_raw(parsed, "biosample", url, acc)

        return normalize_raw(
            {"error": f"no BioSample record for {acc}"}, "biosample",
            f"{ENA_BROWSER_URL}/{acc}", acc,
        )

    def _fetch_ncbi(self, acc: str) -> dict[str, Any] | None:
        params = {"db": "biosample", "id": acc, "retmode": "xml"}
        result = self.http.get(NCBI_EFETCH_URL, params=params)
        if not result.ok:
            return None
        parsed = xml_to_dict(result.text or "<empty/>")
        sample = self._first(parsed.get("BioSampleSet", {}), "BioSample")
        if not isinstance(sample, dict):
            return None
        record = self._parse_ncbi(sample)
        record["_source_url"] = f"{NCBI_EFETCH_URL}?db=biosample&id={acc}"
        return record

    def _fetch_ena(self, acc: str) -> dict[str, Any] | None:
        url = f"{ENA_BROWSER_URL}/{acc}"
        result = self.http.get(url)
        if not result.ok:
            return None
        parsed = xml_to_dict(result.text or "<empty/>")
        sample = self._first(parsed.get("SAMPLE_SET", {}), "SAMPLE")
        if not isinstance(sample, dict):
            return None
        record = self._parse_ena(sample)
        record["_source_url"] = url
        return record

    @staticmethod
    def _first(container: Any, key: str) -> Any:
        if not isinstance(container, dict):
            return None
        value = container.get(key)
        if isinstance(value, list):
            return value[0] if value else None
        return value

    def _parse_ncbi(self, sample: dict[str, Any]) -> dict[str, Any]:
        attributes: list[dict[str, str]] = []
        attrs: dict[str, str] = {}
        for attr in _as_list(sample.get("Attributes", {}).get("Attribute") if isinstance(sample.get("Attributes"), dict) else None):
            if not isinstance(attr, dict):
                continue
            name = attr.get("@harmonized_name") or attr.get("@attribute_name") or ""
            value = attr.get("#text") if isinstance(attr.get("#text"), str) else (attr if isinstance(attr, str) else "")
            self._add_attribute(attributes, attrs, name, value)

        description = sample.get("Description", {}) if isinstance(sample.get("Description"), dict) else {}
        organism = self._ncbi_organism(description)
        if organism:
            attrs.setdefault("organism", organism)
            attrs.setdefault("scientific_name", organism)

        title = description.get("Title") if isinstance(description.get("Title"), str) else None

        links = self._collect_xrefs(sample.get("Ids"), sample.get("Links"))
        aliases = [
            i.get("#text") for i in _as_list(sample.get("Ids", {}).get("Id") if isinstance(sample.get("Ids"), dict) else None)
            if isinstance(i, dict) and isinstance(i.get("#text"), str)
        ]

        return self._assemble(attributes, attrs, organism, title, links, aliases)

    def _parse_ena(self, sample: dict[str, Any]) -> dict[str, Any]:
        attributes: list[dict[str, str]] = []
        attrs: dict[str, str] = {}
        sample_attrs = sample.get("SAMPLE_ATTRIBUTES", {})
        for attr in _as_list(sample_attrs.get("SAMPLE_ATTRIBUTE") if isinstance(sample_attrs, dict) else None):
            if isinstance(attr, dict):
                self._add_attribute(attributes, attrs, attr.get("TAG", ""), attr.get("VALUE", ""))

        organism = None
        sample_name = sample.get("SAMPLE_NAME", {})
        if isinstance(sample_name, dict):
            sci = sample_name.get("SCIENTIFIC_NAME")
            if isinstance(sci, str) and sci.strip():
                organism = sci.strip()
                attrs.setdefault("organism", organism)
                attrs.setdefault("scientific_name", organism)

        title = sample.get("TITLE") if isinstance(sample.get("TITLE"), str) else None

        xref_text = [sample.get("@accession", ""), sample.get("@alias", "")]
        identifiers = sample.get("IDENTIFIERS", {})
        if isinstance(identifiers, dict):
            for ext in _as_list(identifiers.get("EXTERNAL_ID")):
                if isinstance(ext, dict) and isinstance(ext.get("#text"), str):
                    xref_text.append(ext["#text"])
        links = self._extract_accessions(" ".join(t for t in xref_text if t))

        alias = sample.get("@alias")
        aliases = [alias] if isinstance(alias, str) and alias else []

        return self._assemble(attributes, attrs, organism, title, links, aliases)

    def _assemble(
        self,
        attributes: list[dict[str, str]],
        attrs: dict[str, str],
        organism: str | None,
        title: str | None,
        links: list[str],
        aliases: list[str],
    ) -> dict[str, Any]:
        record: dict[str, Any] = {"_attributes": attrs, "attributes": attributes, "_links": links}
        if organism:
            record["Organism"] = organism
        if title:
            record["title"] = title
        if aliases:
            record["aliases"] = aliases
        return record

    @staticmethod
    def _add_attribute(
        attributes: list[dict[str, str]], attrs: dict[str, str], name: str, value: Any
    ) -> None:
        if not name or not isinstance(value, str) or not value.strip():
            return
        key = _norm_key(name)
        val = value.strip()
        attributes.append({"name": key, "value": val})
        # First-wins so a repeated tag never clobbers an earlier value; the full
        # occurrence list is preserved in ``attributes``.
        attrs.setdefault(key, val)

    @staticmethod
    def _ncbi_organism(description: dict[str, Any]) -> str | None:
        organism = description.get("Organism", {})
        if isinstance(organism, dict):
            name = organism.get("@taxonomy_name") or organism.get("OrganismName")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _collect_xrefs(self, ids: Any, links: Any) -> list[str]:
        tokens: list[str] = []
        if isinstance(ids, dict):
            for i in _as_list(ids.get("Id")):
                if isinstance(i, dict) and isinstance(i.get("#text"), str):
                    tokens.append(i["#text"])
        if isinstance(links, dict):
            for link in _as_list(links.get("Link")):
                if isinstance(link, dict) and isinstance(link.get("#text"), str):
                    tokens.append(link["#text"])
                elif isinstance(link, str):
                    tokens.append(link)
        return self._extract_accessions(" ".join(tokens))

    @staticmethod
    def _extract_accessions(text: str) -> list[str]:
        seen: list[str] = []
        for match in _XREF_RE.findall(text or ""):
            if match not in seen:
                seen.append(match)
        return seen

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw
