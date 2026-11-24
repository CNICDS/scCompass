"""Order-invariant enrichment content hashing.

The extraction content hash must change when a sibling's metadata changes
(re-trigger extraction) but stay stable when siblings are merely discovered in a
different order. We hash a canonically-sorted bundle
``{primary, siblings sorted by (connector, accession)}`` rather than the raw
discovery sequence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def build_bundle(primary_raw: dict[str, Any], sibling_raws: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build a deterministic enrichment bundle for hashing.

    ``sibling_raws`` are normalized raw records (``normalize_raw`` output) with
    ``source``/``accession``/``raw``; they are sorted by ``(connector,
    accession)`` so discovery order does not affect the bundle.
    """
    siblings = sorted(
        (
            {
                "connector": str(s.get("source", "")),
                "accession": str(s.get("accession", "")),
                "raw": s.get("raw", {}),
            }
            for s in sibling_raws
        ),
        # Content tiebreaker keeps the bundle order-invariant even if two entries
        # share a (connector, accession) key with different payloads.
        key=lambda s: (
            s["connector"],
            s["accession"],
            json.dumps(s["raw"], sort_keys=True, ensure_ascii=False, default=str),
        ),
    )
    return {"primary": primary_raw, "siblings": siblings}


def content_hash(primary_raw: dict[str, Any], sibling_raws: Iterable[dict[str, Any]] = ()) -> str:
    """SHA-256 over the canonical enrichment bundle."""
    bundle = build_bundle(primary_raw, sibling_raws)
    payload = json.dumps(bundle, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
