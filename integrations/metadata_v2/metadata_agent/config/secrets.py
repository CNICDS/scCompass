from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_SECRET_KEYS = ("NCBI_API_KEY", "NCBI_API_KEYS", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MODEL_API_KEY")
_REDACTION_PATTERN: re.Pattern | None = None


def _build_redaction_pattern() -> re.Pattern | None:
    values = []
    for key in _SECRET_KEYS:
        val = os.environ.get(key, "")
        if val:
            for part in val.split(","):
                part = part.strip()
                if len(part) >= 8:
                    values.append(re.escape(part))
    if not values:
        return None
    return re.compile("|".join(values))


def get_redaction_pattern() -> re.Pattern | None:
    global _REDACTION_PATTERN
    if _REDACTION_PATTERN is None:
        _REDACTION_PATTERN = _build_redaction_pattern()
    return _REDACTION_PATTERN


def redact(text: str) -> str:
    pattern = get_redaction_pattern()
    if pattern is None:
        return text
    return pattern.sub("[REDACTED]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact(str(v)) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redact(str(a)) if isinstance(a, str) else a for a in record.args)
        return True


def get_ncbi_api_keys() -> list[str]:
    pool_str = os.environ.get("NCBI_API_KEYS", "")
    if pool_str:
        keys = [k.strip() for k in pool_str.split(",") if k.strip()]
        if keys:
            return keys
    single = os.environ.get("NCBI_API_KEY", "")
    if single.strip():
        return [single.strip()]
    return []


def get_anthropic_api_key() -> str | None:
    val = os.environ.get("ANTHROPIC_API_KEY", "")
    return val.strip() if val.strip() else None


def get_llm_api_key(api_mode: str) -> str | None:
    specific = "ANTHROPIC_API_KEY" if api_mode == "messages" else "OPENAI_API_KEY"
    for name in ("MODEL_API_KEY", specific):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


_warned_no_key = False


def ncbi_rate_limit() -> int:
    global _warned_no_key
    keys = get_ncbi_api_keys()
    if keys:
        return 10
    if not _warned_no_key:
        logger.warning("No NCBI API key configured; operating at 3 req/s keyless tier")
        _warned_no_key = True
    return 3
