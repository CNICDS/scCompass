from __future__ import annotations

SOURCE_MAP: dict[str, str] = {
    "geo": "NCBI",
    "sra": "SRA",
    "arrayexpress": "arrayexpress",
    "ena": "ENA",
    "gsa": "GSA",
    "biosample": "BioSample",
}

VALID_SOURCES = set(SOURCE_MAP.values())


def connector_to_source(connector_name: str) -> str:
    source = SOURCE_MAP.get(connector_name)
    if source is None:
        raise ValueError(f"Unknown connector: {connector_name}. Valid: {list(SOURCE_MAP.keys())}")
    return source


def validate_source(source: str) -> bool:
    return source in VALID_SOURCES
