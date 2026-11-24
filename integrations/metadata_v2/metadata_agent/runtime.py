"""Shared concurrency primitives and per-sample timing scopes."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Hashable, Iterator


class KeyedLocks:
    """Coalesce identical requests without serializing unrelated keys."""

    def __init__(self):
        self._guard = threading.Lock()
        self._entries: dict[Hashable, list] = {}

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        with self._guard:
            entry = self._entries.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if not entry[1]:
                    del self._entries[key]


_metrics: ContextVar[dict[str, float] | None] = ContextVar("sample_metrics", default=None)


@contextmanager
def collect_metrics() -> Iterator[dict[str, float]]:
    values: dict[str, float] = defaultdict(float)
    token = _metrics.set(values)
    started = time.perf_counter()
    try:
        yield values
    finally:
        values["total"] = time.perf_counter() - started
        _metrics.reset(token)


@contextmanager
def timed(stage: str) -> Iterator[None]:
    values = _metrics.get()
    if values is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        values[stage] += time.perf_counter() - started


def count(metric: str, amount: int = 1) -> None:
    values = _metrics.get()
    if values is not None:
        values[metric] += amount
