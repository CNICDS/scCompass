"""Source-backed repair of an existing canonical row.

Retain fields outside the reviewed corrections. This is deliberately separate
from a fresh model extraction, and callers must record that distinction in the
export manifest rather than stamping an old row as fully re-extracted.
"""
from __future__ import annotations

from typing import Any

from metadata_agent.config.loader import ProfileSchema
from metadata_agent.extract.pipeline import coerce_numeric_fields
from metadata_agent.extract.source_evidence import reconcile_sample_evidence
from metadata_agent.extract.tiers import TierExtractor
from metadata_agent.store.canonical import defers_canonical_write


def repair_record(
    record: dict[str, Any], raw_record: dict[str, Any], schema: ProfileSchema,
    tiers: TierExtractor, connector_id: str,
) -> dict[str, Any]:
    result = dict(record)
    # These categories were derived by the faulty heuristic, so they must be
    # recomputed even if an older export contains an apparently complete value.
    result["Assay"] = None
    result["RNASource"] = None
    result = tiers.acquire(result, raw_record, connector_id)
    if defers_canonical_write(result.get("_status")):
        raise ValueError(f"Repair incomplete for {record.get('SourceID')}: {result['_status']}")
    result = reconcile_sample_evidence(result, raw_record, schema, connector_id)
    audit = list(result.get("_audit_log", []))
    result = coerce_numeric_fields(result, schema, audit)
    result["_field_evidence"] = [
        {**item, "normalized_value": str(result[item["field_name"]])
         if result.get(item["field_name"]) is not None else ""}
        for item in result.get("_field_evidence", [])
    ]
    result["_audit_log"] = audit
    return result
