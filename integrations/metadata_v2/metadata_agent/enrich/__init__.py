"""Cross-link enrichment: resolve sibling accessions across databases and
field-fill each sample's canonical row without touching identity columns.
"""
from __future__ import annotations

from metadata_agent.enrich.bundle import build_bundle, content_hash
from metadata_agent.enrich.merge import is_fillable, merge_enrichment, sibling_priority
from metadata_agent.enrich.resolver import (
    CrossLinkResolver,
    EnrichmentResult,
    SiblingRef,
    enrich_record,
)

__all__ = [
    "CrossLinkResolver",
    "EnrichmentResult",
    "SiblingRef",
    "enrich_record",
    "merge_enrichment",
    "is_fillable",
    "sibling_priority",
    "build_bundle",
    "content_hash",
]
