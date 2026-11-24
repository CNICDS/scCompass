from __future__ import annotations

import logging
from typing import Any, Iterator

from metadata_agent.connectors.base import SourceConnector, register_connector
from metadata_agent.connectors.query_builder import resolve_query
from metadata_agent.connectors.xml_helpers import normalize_raw, xml_to_dict

logger = logging.getLogger(__name__)

ENA_PORTAL_URL = "https://www.ebi.ac.uk/ena/portal/api/search"
ENA_BROWSER_URL = "https://www.ebi.ac.uk/ena/browser/api/xml"


@register_connector
class ENAConnector(SourceConnector):
    name = "ena"

    def search(self, since: str | None = None, until: str | None = None) -> Iterator[str]:
        query = resolve_query("ena", self.config)
        date_field = self.config.get("date_field", "last_updated")

        date_filter = ""
        if since and until:
            date_filter = f" AND {date_field}>={since} AND {date_field}<={until}"
        elif since:
            date_filter = f" AND {date_field}>={since}"
        elif until:
            date_filter = f" AND {date_field}<={until}"

        full_query = query + date_filter if query else date_filter.lstrip(" AND ")

        params: dict[str, Any] = {
            "result": "sample",
            "query": full_query,
            "fields": "accession",
            "format": "tsv",
            "limit": 0,
        }

        result = self.http.get(ENA_PORTAL_URL, params=params)
        if not result.ok:
            logger.error("ENA Portal search failed: %s", result.error)
            return

        lines = (result.text or "").strip().splitlines()
        if len(lines) <= 1:
            logger.info("ENA portal query=%r yielded 0 accessions", full_query)
            return

        seen: set[str] = set()
        for line in lines[1:]:
            acc = line.strip()
            if acc and acc not in seen:
                seen.add(acc)
                yield acc
        logger.info("ENA portal query=%r yielded %d unique accessions", full_query, len(seen))

    def fetch(self, accession: str) -> dict[str, Any]:
        url = f"{ENA_BROWSER_URL}/{accession}"
        result = self.http.get(url)
        if not result.ok:
            return normalize_raw(
                {"error": result.error}, "ena", url, accession,
            )

        parsed = xml_to_dict(result.text or "<empty/>")

        # ERX/experiment accessions return an EXPERIMENT_SET; the sample-level
        # characteristics (organism, organ, sex, dev stage, ...) live in the
        # linked ERS sample record, so follow that link and merge it in. ERS
        # accessions return a SAMPLE_SET directly.
        if "EXPERIMENT_SET" in parsed:
            parsed = self._enrich_experiment(parsed)
        else:
            parsed["_attributes"] = self._extract_sample_attributes(parsed)

        return normalize_raw(parsed, "ena", url, accession)

    def fetch_by_accession(self, acc: str) -> dict[str, Any]:
        """Fetch any ENA accession type (ERR/ERX/ERS/ERP) via the Browser API.

        The Browser XML endpoint resolves run/experiment/sample/study
        accessions uniformly, so this delegates to the existing fetch path.
        """
        return self.fetch(acc)

    def _enrich_experiment(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Populate ``_attributes`` for an EXPERIMENT_SET by following its
        SAMPLE_DESCRIPTOR link to the ERS sample record.

        Also lifts the study accession and experiment title so ``Series ID``
        and the label fallback have something to resolve against.
        """
        experiment = parsed.get("EXPERIMENT_SET", {}).get("EXPERIMENT", {})
        if isinstance(experiment, list):
            experiment = experiment[0] if experiment else {}
        if not isinstance(experiment, dict):
            experiment = {}

        sample_acc: str | None = None
        study_acc: str | None = None
        title = experiment.get("TITLE")

        study_ref = experiment.get("STUDY_REF", {})
        if isinstance(study_ref, dict):
            study_acc = study_ref.get("@accession")

        design = experiment.get("DESIGN", {})
        if isinstance(design, dict):
            descriptor = design.get("SAMPLE_DESCRIPTOR", {})
            if isinstance(descriptor, dict):
                sample_acc = descriptor.get("@accession")

        attrs: dict[str, str] = {}
        if sample_acc:
            sample_parsed = self._fetch_sample_xml(sample_acc)
            if sample_parsed is not None:
                attrs = self._extract_sample_attributes(sample_parsed)

        parsed["_attributes"] = attrs
        if study_acc:
            parsed["study_accession"] = study_acc
        if isinstance(title, str) and title.strip():
            parsed["title"] = title.strip()
        if sample_acc:
            parsed["sample_accession"] = sample_acc
        return parsed

    def _fetch_sample_xml(self, sample_accession: str) -> dict[str, Any] | None:
        """Fetch and parse an ERS sample record. Returns ``None`` on failure so
        the experiment record is still stored, just without sample attributes.
        """
        url = f"{ENA_BROWSER_URL}/{sample_accession}"
        result = self.http.get(url)
        if not result.ok:
            logger.warning(
                "ENA sample fetch failed for %s: %s", sample_accession, result.error
            )
            return None
        return xml_to_dict(result.text or "<empty/>")

    def _extract_sample_attributes(self, parsed: dict[str, Any]) -> dict[str, str]:
        attrs: dict[str, str] = {}
        try:
            sample_set = parsed.get("SAMPLE_SET", parsed)
            sample = sample_set.get("SAMPLE", {})
            if isinstance(sample, list):
                sample = sample[0]
            sample_attrs = (
                sample.get("SAMPLE_ATTRIBUTES", {}).get("SAMPLE_ATTRIBUTE", [])
            )
            if isinstance(sample_attrs, dict):
                sample_attrs = [sample_attrs]
            for attr in sample_attrs:
                tag = attr.get("TAG", "")
                value = attr.get("VALUE", "")
                if tag and isinstance(value, str):
                    attrs[tag] = value

            # SAMPLE_NAME carries the canonical organism even when the free-text
            # SAMPLE_ATTRIBUTE tags omit it.
            sample_name = sample.get("SAMPLE_NAME", {})
            if isinstance(sample_name, dict):
                sci = sample_name.get("SCIENTIFIC_NAME")
                if isinstance(sci, str) and sci.strip():
                    attrs.setdefault("scientific_name", sci.strip())
                common = sample_name.get("COMMON_NAME")
                if isinstance(common, str) and common.strip():
                    attrs.setdefault("common name", common.strip())
        except (AttributeError, TypeError, KeyError):
            pass
        return attrs

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw
