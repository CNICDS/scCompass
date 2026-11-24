from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate: float, capacity: float | None = None):
        self._rate = rate
        self._capacity = capacity or 1.0
        self._tokens = 1.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


class RateLimiterRegistry:
    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def get_or_create(self, host: str, rate: float) -> TokenBucket:
        with self._lock:
            if host not in self._buckets:
                self._buckets[host] = TokenBucket(rate)
            return self._buckets[host]

    def acquire(self, host: str, rate: float) -> None:
        bucket = self.get_or_create(host, rate)
        bucket.acquire()
