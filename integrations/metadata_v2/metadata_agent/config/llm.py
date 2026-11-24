"""Resolve the model transport without reading or embedding credentials."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

API_PATHS = {"messages": "messages", "responses": "responses", "chat_completions": "chat/completions"}


def resolve_endpoint(api_mode: str = "auto", base_url: str | None = None) -> tuple[str, str]:
    mode = api_mode.strip().lower()
    if mode not in {"auto", *API_PATHS}:
        raise ValueError("LLM api_mode must be auto, messages, responses, or chat_completions")
    parsed = urlsplit(base_url.strip().rstrip("/")) if base_url else None
    if parsed:
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("LLM base_url must be an HTTP(S) URL without credentials, query, or fragment")
        path = parsed.path.rstrip("/")
        suffix = path.rsplit("/", 1)[-1]
        if suffix == "response":
            raise ValueError("Use the standard /v1/responses endpoint (plural)")
        inferred = next((name for name, tail in API_PATHS.items() if path.endswith("/" + tail)), None)
        if inferred:
            if mode != "auto" and mode != inferred:
                raise ValueError("LLM api_mode conflicts with the full endpoint URL")
            mode = inferred
            path = path.removesuffix("/" + API_PATHS[inferred])
        if not path:
            path = "/v1"
        if not path.endswith("/v1"):
            raise ValueError("LLM base_url must be a host, an API root ending in /v1, or a full endpoint")
        root = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))
    else:
        root = "https://api.anthropic.com/v1" if mode in {"auto", "messages"} else "https://api.openai.com/v1"
    return ("messages" if mode == "auto" else mode), root


@dataclass(frozen=True)
class LLMSettings:
    model: str
    api_mode: str
    base_url: str

    @property
    def cache_identity(self) -> str:
        return json.dumps({"model": self.model, "api_mode": self.api_mode,
                           "base_url": self.base_url}, sort_keys=True)


def resolve_llm_settings(config: dict[str, Any] | None = None, *, api_mode: str | None = None) -> LLMSettings:
    settings = (config or {}).get("llm", {})
    mode, root = resolve_endpoint(
        api_mode or os.environ.get("MODEL_API_MODE") or settings.get("api_mode") or "auto",
        os.environ.get("MODEL_BASE_URL") or settings.get("base_url"),
    )
    model = (os.environ.get("MODEL") or settings.get("model") or "").strip()
    if not model:
        if mode != "messages":
            raise ValueError("Set MODEL or llm.model to a model supported by your configured endpoint")
        model = "claude-sonnet-4-20250514"
    return LLMSettings(model, mode, root)
