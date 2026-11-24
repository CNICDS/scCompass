from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator


class SourceConnector(ABC):
    name: str

    def __init__(self, config: dict[str, Any], http_client: Any) -> None:
        self.config = config
        self.http = http_client

    @abstractmethod
    def search(self, since: str | None = None, until: str | None = None) -> Iterator[str]:
        """Yield accession IDs matching the configured query within the date window."""

    @abstractmethod
    def fetch(self, accession: str) -> dict[str, Any]:
        """Fetch raw metadata for a single accession. Returns normalized raw_dict with provenance."""

    @abstractmethod
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize raw fetched data into a standardized dict for storage."""


_REGISTRY: dict[str, type[SourceConnector]] = {}


def register_connector(cls: type[SourceConnector]) -> type[SourceConnector]:
    _REGISTRY[cls.name] = cls
    return cls


def get_connector(name: str) -> type[SourceConnector]:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown connector: {name}. Available: {list(_REGISTRY.keys())}")
    return _REGISTRY[name]


def all_connectors() -> dict[str, type[SourceConnector]]:
    return dict(_REGISTRY)
