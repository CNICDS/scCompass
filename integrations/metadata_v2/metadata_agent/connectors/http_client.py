from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from metadata_agent.config.secrets import ncbi_rate_limit, redact
from metadata_agent.connectors.key_pool import NCBIKeyPool
from metadata_agent.connectors.rate_limiter import RateLimiterRegistry

logger = logging.getLogger(__name__)

NCBI_HOST = "eutils.ncbi.nlm.nih.gov"

_DEFAULT_RATES: dict[str, float] = {
    NCBI_HOST: 3.0,
    "www.ebi.ac.uk": 50.0,
    "ftp.ebi.ac.uk": 10.0,
    "www.ncbi.nlm.nih.gov": 3.0,
    "download.cncb.ac.cn": 5.0,
    "ngdc.cncb.ac.cn": 5.0,
}


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    text: str | None = None
    error: str | None = None
    attempts: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def failed(self) -> bool:
        return self.error is not None


class HttpClient:
    def __init__(
        self,
        rate_registry: RateLimiterRegistry | None = None,
        key_pool: NCBIKeyPool | None = None,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        timeout: float = 30.0,
    ):
        self._rate_registry = rate_registry or RateLimiterRegistry()
        self._key_pool = key_pool or NCBIKeyPool()
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "metadata-agent/0.1.0"

    def _host_rate(self, host: str) -> float:
        if host == NCBI_HOST or host == "www.ncbi.nlm.nih.gov":
            return float(ncbi_rate_limit())
        return _DEFAULT_RATES.get(host, 5.0)

    def close(self) -> None:
        self._session.close()

    def _inject_ncbi_key(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        params = dict(params) if params else {}
        if NCBI_HOST in url or "www.ncbi.nlm.nih.gov" in url:
            key = self._key_pool.next_key()
            if key:
                params["api_key"] = key
        return params

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        rate = self._host_rate(host)
        params = self._inject_ncbi_key(url, params)

        result = FetchResult(url=url)
        for attempt in range(self._max_attempts):
            self._rate_registry.acquire(host, rate)
            result.attempts = attempt + 1
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
                result.status = resp.status_code
                result.headers = dict(resp.headers)

                if resp.ok:
                    result.text = resp.text
                    # Requests defaults some text responses to Latin-1 even
                    # when GEO serves UTF-8, corrupting primes and apostrophes.
                    if (resp.encoding or "").casefold() in {"iso-8859-1", "latin-1"}:
                        content = getattr(resp, "content", None)
                        if isinstance(content, bytes):
                            try:
                                result.text = content.decode("utf-8")
                            except UnicodeDecodeError:
                                pass
                    return result

                if resp.status_code in (429, 500, 502, 503, 504):
                    delay = self._base_delay * (2 ** attempt)
                    logger.warning(
                        "HTTP %d from %s (attempt %d/%d), retrying in %.1fs",
                        resp.status_code, redact(url), attempt + 1, self._max_attempts, delay,
                    )
                    time.sleep(delay)
                    continue

                result.error = f"HTTP {resp.status_code}"
                return result

            except requests.RequestException as exc:
                delay = self._base_delay * (2 ** attempt)
                logger.warning(
                    "Request error for %s (attempt %d/%d): %s, retrying in %.1fs",
                    redact(url), attempt + 1, self._max_attempts, exc, delay,
                )
                time.sleep(delay)

        result.error = f"Max retries ({self._max_attempts}) exceeded"
        return result
