"""Shared NCBI E-utilities paging — retrieve every UID for an ``esearch`` that
exceeds the 10000-per-call cap, with no silent truncation.

``esearch`` returns at most 10000 IDs per call. To obtain the full set for a
larger query we snapshot it server-side with ``usehistory=y`` (capturing
``WebEnv``/``query_key``) and page through it in ``retstart`` windows no larger
than 10000, deduplicating UIDs. If the unique count retrieved falls short of the
reported ``esearchresult.count``, we log a bounded-result warning **with the
dropped count** rather than reporting the partial set as complete.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

# NCBI's hard per-call ceiling on returned IDs.
MAX_PAGE_SIZE = 10000


def _parse_esearchresult(text: str | None) -> dict[str, Any] | None:
    try:
        return json.loads(text or "{}").get("esearchresult", {})
    except (ValueError, TypeError):
        return None


def esearch_all(
    http: Any,
    base_params: dict[str, Any],
    *,
    page_size: int = MAX_PAGE_SIZE,
    label: str = "esearch",
) -> Iterator[str]:
    """Yield every UID matching an NCBI ``esearch``, paging past the 10k cap.

    ``base_params`` supplies ``db``/``term`` and any date predicates
    (``datetype``/``mindate``/``maxdate``); this helper owns
    ``retmode``/``usehistory``/``retmax``/``retstart``/``WebEnv``. UIDs are
    deduplicated across pages. A shortfall against ``esearchresult.count`` is
    logged as a bounded-result warning; it is never reported as success.
    """
    page_size = min(page_size, MAX_PAGE_SIZE)
    params = {k: v for k, v in base_params.items() if v is not None}

    init = dict(params, retmode="json", usehistory="y", retmax=0, retstart=0)
    first = http.get(ESEARCH_URL, params=init)
    if not first.ok:
        logger.error("esearch %s failed: %s", label, first.error)
        return

    esr = _parse_esearchresult(first.text)
    if esr is None:
        logger.error("esearch %s returned unparseable JSON", label)
        return

    count = int(esr.get("count", 0) or 0)
    webenv = esr.get("webenv")
    query_key = esr.get("querykey")
    logger.info("esearch %s: query=%r → %d total records", label, params.get("term", ""), count)
    if count == 0:
        return

    seen: set[str] = set()
    retstart = 0
    while retstart < count:
        page = dict(params, retmode="json", retmax=page_size, retstart=retstart)
        if webenv and query_key:
            page["WebEnv"] = webenv
            page["query_key"] = query_key

        res = http.get(ESEARCH_URL, params=page)
        if not res.ok:
            logger.warning(
                "esearch %s page retstart=%d failed: %s", label, retstart, res.error
            )
            break

        esr_page = _parse_esearchresult(res.text)
        ids = esr_page.get("idlist", []) if esr_page else []
        if not ids:
            break

        for uid in ids:
            if uid not in seen:
                seen.add(uid)
                yield uid
        retstart += page_size

    logger.info("esearch %s: %d unique UIDs yielded of %d", label, len(seen), count)
    if len(seen) != count:
        logger.warning(
            "esearch %s bounded result: retrieved %d unique of %d (dropped %d)",
            label, len(seen), count, count - len(seen),
        )
