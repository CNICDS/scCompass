from __future__ import annotations

import itertools
import threading

from metadata_agent.config.secrets import get_ncbi_api_keys


class NCBIKeyPool:
    def __init__(self) -> None:
        keys = get_ncbi_api_keys()
        self._keys = keys
        self._cycle = itertools.cycle(keys) if keys else None
        self._lock = threading.Lock()

    @property
    def has_keys(self) -> bool:
        return bool(self._keys)

    @property
    def count(self) -> int:
        return len(self._keys)

    def next_key(self) -> str | None:
        if self._cycle is None:
            return None
        with self._lock:
            return next(self._cycle)
