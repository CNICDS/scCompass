from __future__ import annotations

import json
import math
import os
import logging
from pathlib import Path
from typing import Any

import requests

from metadata_agent.config.llm import API_PATHS, resolve_endpoint
from metadata_agent.config.loader import (
    ColumnDef,
    ProfileSchema,
    load_profile_schema,
    load_vocab,
)
from metadata_agent.config.secrets import get_llm_api_key, redact
from metadata_agent.store.canonical import PENDING_LLM_INVALID_RESPONSE, PENDING_LLM_TRUNCATED, PRIMARY_KEY

logger = logging.getLogger(__name__)


def _model_read_timeout_seconds() -> float:
    timeout = float(os.environ.get("MODEL_READ_TIMEOUT_SECONDS", "180"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MODEL_READ_TIMEOUT_SECONDS must be a finite positive number")
    return timeout

# The identity pair, imported rather than re-declared: a second copy here
# would keep advertising a renamed column to the model.
SYSTEM_COLUMNS = frozenset(PRIMARY_KEY)

_NUMERIC_JSON_TYPES = {"integer": "integer", "real": "number"}

_TOOL_NAME = "structured_output"


def _chat_response_summary(response: Any) -> str:
    """A bounded diagnostic with no response text, keys or arguments."""
    if not isinstance(response, dict):
        return f"response_type={type(response).__name__}"
    choices = response.get("choices")
    if not isinstance(choices, list):
        return f"choices_type={type(choices).__name__}"
    parts = [f"choices={len(choices)}"]
    if len(choices) != 1 or not isinstance(choices[0], dict):
        return "; ".join(parts)
    choice = choices[0]
    finish = choice.get("finish_reason")
    known_finish = ("tool_calls", "function_call", "length", "stop", "content_filter", None)
    parts.append(f"finish_reason={finish if finish in known_finish else 'unrecognized'}")
    message = choice.get("message")
    if not isinstance(message, dict):
        parts.append(f"message_type={type(message).__name__}")
        return "; ".join(parts)
    calls = message.get("tool_calls")
    parts.append(f"tool_calls={len(calls)}" if isinstance(calls, list)
                 else f"tool_calls_type={type(calls).__name__}")
    if isinstance(calls, list):
        types, names = [], []
        for call in calls[:4]:
            kind = call.get("type") if isinstance(call, dict) else "invalid"
            types.append(str(kind) if kind in (None, "function", "custom") else "unrecognized")
            function = call.get("function") if isinstance(call, dict) else None
            names.append("expected" if isinstance(function, dict) and function.get("name") == _TOOL_NAME
                         else "unexpected_or_missing")
        parts.extend((f"tool_types={types}", f"function_names={names}"))
    parts.extend((f"legacy_function_call={isinstance(message.get('function_call'), dict)}",
                  f"content_present={bool(message.get('content'))}", f"refusal={bool(message.get('refusal'))}"))
    return "; ".join(parts)


class LLMResponseError(RuntimeError):
    """A response must remain pending rather than become a completed review."""

    pending_reason = PENDING_LLM_INVALID_RESPONSE


class LLMInvalidResponse(LLMResponseError):
    """A completed response has missing, ambiguous or malformed structured output."""


class LLMTruncated(LLMResponseError):
    """The model stopped before its structured output was complete.

    Raised rather than returned so a partial tool input can never be mistaken
    for an extraction result. A retry at the same budget produces the same
    truncation, so the caller defers the sample instead of retrying.
    """

    pending_reason = PENDING_LLM_TRUNCATED


def _json_type(col: ColumnDef) -> str:
    """JSON Schema type advertised to the model for a column.

    Numeric fields are declared numeric so the model returns a number rather
    than a formatted string. A model that answers "unknown" anyway is handled
    downstream by numeric coercion, which reads it as missing.
    """
    if col.number and col.numeric_kind:
        return _NUMERIC_JSON_TYPES.get(col.numeric_kind, "string")
    return "string"


def _build_input_schema(schema: ProfileSchema, profile_dir: Path) -> dict[str, Any]:
    vocab_dir = profile_dir / "vocab"
    properties: dict[str, Any] = {}
    required: list[str] = []

    for col in schema.columns:
        if col.name in SYSTEM_COLUMNS or col.system:
            continue
        if col.llm_mode == "disabled":
            continue

        prop: dict[str, Any] = {"type": _json_type(col)}
        if col.description:
            prop["description"] = col.description
        if schema.review_policy:
            prop["type"] = [_json_type(col), "null"]
            if col.number:
                prop = {"anyOf": [prop, {"const": "null"}], "description": col.description}

        if (col.type == "enum" or (col.type == "semi" and not schema.review_policy)) and col.enum_ref:
            vocab_path = vocab_dir / col.enum_ref
            if vocab_path.exists():
                values = load_vocab(vocab_path)
                prop["enum"] = values + (["", "null", None] if schema.review_policy else [])

        properties[col.name] = prop

        if col.required and col.name not in SYSTEM_COLUMNS and not schema.review_policy:
            required.append(col.name)

    if schema.review_policy:
        properties["_evidence"] = {
            "type": "object",
            "description": "Exact raw-source quote supporting each nonempty field, including confirmed absence.",
            "properties": {name: {"type": "string"} for name in properties},
            "additionalProperties": False,
        }
        required.append("_evidence")

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _trigger_fields_from_schema(schema: ProfileSchema) -> list[str]:
    return [
        col.name for col in schema.columns
        if col.type in ("enum", "semi") and not col.system and col.name not in SYSTEM_COLUMNS
    ]


def needs_llm_fallback(extracted: dict[str, Any], trigger_fields: list[str]) -> bool:
    for field in trigger_fields:
        val = extracted.get(field)
        if val is None or val == "" or val == "unknown":
            return True
    return False


class LLMExtractor:
    def __init__(
        self,
        profile_dir: str | Path,
        max_calls: int = 100,
        model: str = "claude-sonnet-4-20250514",
        base_url: str | None = None,
        api_mode: str = "auto",
    ):
        self._profile_dir = Path(profile_dir)
        self._schema = load_profile_schema(self._profile_dir)
        self._input_schema = _build_input_schema(self._schema, self._profile_dir)
        self._trigger_fields = _trigger_fields_from_schema(self._schema)
        self._prompt_template = self._load_prompt()
        self._max_calls = max_calls
        self._model = model
        self._api_mode, self._api_root = resolve_endpoint(api_mode, base_url)
        # Anthropic's SDK appends /v1/messages itself.
        self._base_url = self._api_root.removesuffix("/v1") if base_url else None
        self._calls_made = 0
        self._sample_calls_start = 0
        self._last_failure_reason: str | None = None
        self._client = None
        self._session: requests.Session | None = None

    def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close:
                close()
            self._client = None
        if self._session is not None:
            self._session.close()
            self._session = None

    def _load_prompt(self) -> str:
        prompt_path = self._profile_dir / "prompt.md"
        return prompt_path.read_text(encoding="utf-8")

    @property
    def calls_made(self) -> int:
        return self._calls_made

    @property
    def budget_exhausted(self) -> bool:
        return self._calls_made - self._sample_calls_start >= self._max_calls

    def begin_sample(self) -> None:
        """Reset this sample's allowance while retaining clients and total usage."""
        self._sample_calls_start = self._calls_made

    @property
    def last_failure_reason(self) -> str | None:
        """A non-sensitive cause for the latest call that produced no result."""
        return self._last_failure_reason

    @property
    def api_key_env_names(self) -> str:
        return "MODEL_API_KEY or " + ("ANTHROPIC_API_KEY" if self._api_mode == "messages" else "OPENAI_API_KEY")

    @property
    def trigger_fields(self) -> list[str]:
        return self._trigger_fields

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_schema

    def extract(
        self,
        raw_text: str,
        schema_json: str,
        vocab_summary: str,
    ) -> dict[str, Any] | None:
        """Extract one record, or ``None`` when no result could be obtained.

        Raises:
            LLMInvalidResponse: Chat Completions returned malformed or ambiguous
                structured output; the review must remain pending.
            LLMTruncated: the response did not stop on a complete tool call, so
                whatever it carries is partial and must be deferred. ``None``
                means no result was obtained; review profiles defer that too.
        """
        self._last_failure_reason = None
        if self.budget_exhausted:
            self._last_failure_reason = "budget_exhausted"
            logger.warning("Sample LLM call budget exhausted (%d/%d)",
                           self._calls_made - self._sample_calls_start, self._max_calls)
            return None

        api_key = get_llm_api_key(self._api_mode)
        if not api_key:
            self._last_failure_reason = "missing_api_key"
            logger.warning("%s not set; marking sample as pending", self.api_key_env_names)
            return None

        prompt = self._prompt_template.replace("{schema}", schema_json).replace("{vocab}", vocab_summary)
        content = f"{prompt}\n\n---\nRaw metadata:\n{raw_text}"
        try:
            if self._api_mode == "responses":
                response = self._request_responses(content, api_key)
            elif self._api_mode == "chat_completions":
                response = self._request_chat(content, api_key)
            else:
                response = self._request_messages(content, api_key)
        except Exception as exc:
            self._last_failure_reason = "request_failed"
            logger.error("LLM extraction failed (%s): %s", self._api_mode, redact(str(exc)))
            return None

        if self._api_mode == "responses":
            return self._accept_responses(response)
        if self._api_mode == "chat_completions":
            return self._accept_chat(response)
        return self._accept(response)

    def _request_messages(self, content: str, api_key: str) -> Any:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=api_key,
                **({"base_url": self._base_url} if self._base_url else {}),
            )
        self._calls_made += 1
        return self._client.messages.create(
            model=self._model, max_tokens=8192,
            messages=[{"role": "user", "content": content}],
            tools=[{"name": _TOOL_NAME, "description": "Output the extracted metadata fields",
                    "input_schema": self._input_schema}],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
        )

    def _request_responses(self, content: str, api_key: str) -> dict[str, Any]:
        payload = {
            "model": self._model, "max_output_tokens": 8192,
            "input": [{"role": "user", "content": content}],
            "tools": [{"type": "function", "name": _TOOL_NAME,
                       "description": "Output the extracted metadata fields",
                       "parameters": self._input_schema, "strict": False}],
            "tool_choice": {"type": "function", "name": _TOOL_NAME},
            "parallel_tool_calls": False, "store": False, "stream": False,
        }
        return self._request_json(payload, api_key)

    def _request_chat(self, content: str, api_key: str) -> dict[str, Any]:
        return self._request_json({
            "model": self._model, "max_completion_tokens": 8192,
            "messages": [{"role": "user", "content": content}],
            "tools": [{"type": "function", "function": {
                "name": _TOOL_NAME, "description": "Output the extracted metadata fields",
                "parameters": self._input_schema, "strict": False,
            }}],
            "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
            "parallel_tool_calls": False, "stream": False,
        }, api_key)

    def _request_json(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        if self._session is None:
            self._session = requests.Session()
        self._calls_made += 1
        # Use the existing HTTP dependency; no OpenAI SDK is needed. A failure
        # follows the normal pending/retry path and never switches protocols.
        with self._session.post(
            f"{self._api_root}/{API_PATHS[self._api_mode]}", json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=(10, _model_read_timeout_seconds()), allow_redirects=False,
        ) as response:
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"LLM endpoint returned HTTP {response.status_code}")
            return response.json()

    def _accept_responses(self, response: Any) -> dict[str, Any]:
        if not isinstance(response, dict) or response.get("status") != "completed":
            raise LLMTruncated("incomplete Responses output: response status is not completed")
        if response.get("error") or response.get("incomplete_details"):
            raise LLMTruncated("Responses output includes error/incomplete_details")
        output = response.get("output")
        if not isinstance(output, list):
            raise LLMTruncated("Responses output is not a list")
        calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
        if (len(calls) != 1 or calls[0].get("name") != _TOOL_NAME
                or calls[0].get("status") not in (None, "completed")):
            raise LLMTruncated("Responses output must contain one completed structured_output function call")
        return self._parse_arguments(calls[0].get("arguments"))

    def _accept_chat(self, response: Any) -> dict[str, Any]:
        # Log structural facts only: never arguments, free text, refusal text
        # or untrusted names from a gateway response.
        summary = _chat_response_summary(response)

        def reject(message: str, error_type=LLMInvalidResponse):
            raise error_type(f"{message} ({summary})")

        if not isinstance(response, dict) or response.get("error"):
            reject("invalid Chat Completions response")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            reject("Chat Completions must return exactly one choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            reject("Chat Completions reached its output token limit", LLMTruncated)
        if finish_reason not in ("tool_calls", "function_call"):
            reject("Chat Completions did not finish with a function call")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("refusal"):
            reject("Chat Completions output is missing a message or contains a refusal")
        calls = message.get("tool_calls")
        legacy = message.get("function_call")
        compatibility = None
        if calls is None or calls == []:
            # Deprecated but documented Chat Completions envelope. Some
            # gateways translate tools into this older form.
            if not isinstance(legacy, dict):
                reject("Chat Completions returned no function call")
            function = legacy
            compatibility = "legacy function_call"
        else:
            if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
                reject("Chat Completions must return exactly one tool call")
            if legacy is not None:
                reject("Chat Completions returned both tool_calls and function_call")
            call = calls[0]
            if call.get("type") not in (None, "function") or call.get("custom") is not None:
                reject("Chat Completions returned a non-function tool call")
            if call.get("type") is None:
                compatibility = "omitted function tool type"
            function = call.get("function")
        if not isinstance(function, dict) or function.get("name") != _TOOL_NAME:
            reject("Chat Completions returned an unexpected or missing function")
        try:
            result = self._parse_arguments(function.get("arguments"))
        except LLMTruncated as exc:
            # With a completion marker this is a malformed payload, not proof
            # that the model hit its output token limit.
            reject(str(exc))
        if compatibility:
            logger.info("Accepted Chat Completions gateway format: %s", compatibility)
        return result

    @staticmethod
    def _parse_arguments(arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, str):
            raise LLMTruncated("Function arguments must be JSON text")
        try:
            result = json.loads(arguments)
        except ValueError as exc:
            raise LLMTruncated("Function arguments are not complete JSON") from exc
        if not isinstance(result, dict):
            raise LLMTruncated("Function arguments must be an object")
        return result

    def _accept(self, response: Any) -> dict[str, Any]:
        """Return the response's tool input, or reject the whole response.

        A response is complete only when the model stopped *because* it made the
        tool call and made it exactly once. Any other shape — stopping on the
        token ceiling, no tool block, or two of them — leaves the arguments
        possibly cut mid-object, and a partial object is indistinguishable from
        a sample whose fields are genuinely empty.
        """
        stop_reason = getattr(response, "stop_reason", None)
        blocks = [
            block for block in response.content
            if getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == _TOOL_NAME
        ]

        if stop_reason != "tool_use" or len(blocks) != 1:
            raise LLMTruncated(
                f"incomplete LLM response: stop_reason={stop_reason!r}, "
                f"{len(blocks)} {_TOOL_NAME} block(s)"
            )
        if not isinstance(blocks[0].input, dict):
            raise LLMTruncated("Messages tool input must be an object")
        return blocks[0].input
