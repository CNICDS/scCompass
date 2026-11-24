"""Tier roster extraction from config.yaml.

The tier configuration in config.yaml declares which fields belong to which
extraction tier. The roster (union of all tier field lists) is required by the
profile validator when any column declares `acquisition: tiered`.
"""
from __future__ import annotations

from typing import Any


def tiered_fields_from_config(config: dict[str, Any]) -> set[str]:
    """Extract the full roster of tiered fields from extraction.tiers config.

    Returns the union of all tier field lists, regardless of tier enabled status.
    An empty config returns an empty set (validator will fail closed if schema
    declares tiered fields but config is missing).
    """
    tiers = config.get("extraction", {}).get("tiers", {})
    fields: set[str] = set()
    for tier_config in tiers.values():
        if isinstance(tier_config, dict):
            tier_fields = tier_config.get("fields", [])
            if isinstance(tier_fields, list):
                fields.update(tier_fields)
    return fields
