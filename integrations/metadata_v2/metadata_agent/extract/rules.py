from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from metadata_agent.config.loader import ProfileSchema

logger = logging.getLogger(__name__)


def load_rules(rules_path: str) -> list[dict[str, Any]]:
    with open(rules_path, "r") as f:
        data = yaml.safe_load(f)
    return data.get("rules", [])


def _resolve_path(data: dict[str, Any], path: str) -> str | None:
    if "." not in path:
        val = data.get(path)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None

    container_key, _, field_key = path.partition(".")
    container = data.get(container_key)
    if container is None:
        return None

    if isinstance(container, dict):
        val = container.get(field_key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None

    if isinstance(container, list):
        field_lower = field_key.lower()
        for item in container:
            if isinstance(item, str) and ":" in item:
                tag, _, value = item.partition(":")
                if tag.strip().lower() == field_lower and value.strip():
                    return value.strip()
            elif isinstance(item, dict):
                val = item.get(field_key)
                if val is None:
                    for k, v in item.items():
                        if k.lower() == field_lower:
                            val = v
                            break
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return None

    return None


def _resolve_values(data: dict[str, Any], path: str) -> list[str]:
    """Read all occurrences of a source field without discarding paragraphs."""
    def strings(value: Any) -> list[str]:
        if isinstance(value, list):
            return [text for item in value for text in strings(item)]
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return [str(value).strip()] if str(value).strip() else []
        return []

    if "." not in path:
        return strings(data.get(path))
    container_key, _, key = path.partition(".")
    container = data.get(container_key)
    if isinstance(container, dict):
        return [text for k, v in container.items() if k.casefold() == key.casefold() for text in strings(v)]
    values = []
    for item in container if isinstance(container, list) else []:
        if isinstance(item, dict):
            values.extend(text for k, v in item.items() if k.casefold() == key.casefold() for text in strings(v))
        elif isinstance(item, str):
            tag, sep, value = item.partition(":")
            if sep and tag.strip().casefold() == key.casefold():
                values.extend(strings(value))
    return values


def _index_value_rules(rules: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_column: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        target = rule.get("target_column")
        if target:
            by_column.setdefault(target, []).append(rule)
    return by_column


def _apply_value_map(
    value: str, rules_for_column: list[dict[str, Any]]
) -> str | None:
    lower_val = value.lower()
    for rule in rules_for_column:
        value_map = rule.get("value_map", {})
        if not value_map:
            continue

        match_regex = rule.get("match_key_regex")
        if match_regex:
            pattern = re.compile(match_regex, re.IGNORECASE)
            if not pattern.search(value):
                continue

        for map_pattern, mapped_value in value_map.items():
            if map_pattern == "default":
                continue
            if re.search(map_pattern, lower_val, re.IGNORECASE):
                return mapped_value

        if "default" in value_map:
            return value_map["default"]

    return value if value.strip() else None


class RulesExtractor:
    def __init__(
        self,
        rules_path: str | list[dict[str, Any]],
        schema: ProfileSchema | None = None,
    ):
        if isinstance(rules_path, list):
            self._rules = rules_path
        else:
            self._rules = load_rules(rules_path)
        self._rules_by_column = _index_value_rules(self._rules)
        self._schema = schema

    def extract(
        self,
        raw_record: dict[str, Any],
        connector_id: str | None = None,
    ) -> dict[str, str]:
        data = raw_record.get("raw", raw_record)
        if not isinstance(data, dict):
            return {}

        if self._schema is not None and connector_id is not None:
            return self._extract_via_source_map(data, connector_id)

        return self._extract_legacy(data)

    def extract_diagnostics(
        self,
        raw_record: dict[str, Any],
        connector_id: str | None = None,
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        data = raw_record.get("raw", raw_record)
        if not isinstance(data, dict):
            return {}, []

        if self._schema is not None and connector_id is not None:
            return self._extract_via_source_map_with_diag(data, connector_id)

        return self._extract_legacy(data), []

    def _extract_via_source_map(
        self, data: dict[str, Any], connector_id: str
    ) -> dict[str, str]:
        result, _ = self._extract_via_source_map_with_diag(data, connector_id)
        return result

    def _extract_via_source_map_with_diag(
        self, data: dict[str, Any], connector_id: str
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        result: dict[str, str] = {}
        diagnostics: list[dict[str, str]] = []

        for col in self._schema.columns:
            if col.system:
                continue
            if not col.source_map:
                continue

            paths = col.source_map.get(connector_id, [])
            if not paths:
                continue

            resolved_candidates: list[tuple[str, str]] = []
            for path in paths:
                values = _resolve_values(data, path)
                if col.source_merge == "all":
                    resolved_candidates.extend((path, value) for value in values)
                elif values:
                    resolved_candidates.append((path, values[0]))

            if not resolved_candidates:
                continue

            column_rules = self._rules_by_column.get(col.name, [])
            best_value: str | None = None
            best_path: str | None = None

            for path, raw_val in resolved_candidates:
                if column_rules:
                    transformed = _apply_value_map(raw_val, column_rules)
                else:
                    transformed = raw_val

                if transformed is None or not transformed.strip():
                    continue

                if best_value is None:
                    best_value = transformed
                    best_path = path
                elif best_value != transformed:
                    if col.source_merge == "all":
                        separator = (col.delimiter + " ") if col.delimiter else "\n\n"
                        if transformed not in best_value.split(separator):
                            best_value += separator + transformed
                        continue
                    diagnostics.append({
                        "column": col.name,
                        "type": "candidate_conflict",
                        "kept_path": best_path or "",
                        "kept_value": best_value,
                        "conflict_path": path,
                        "conflict_value": transformed,
                    })

            if best_value is not None:
                result[col.name] = best_value

        if "OveralDesign" in self._schema.column_names:
            designs = []
            for context in data.get("_series_context", {}).values():
                designs.extend(_resolve_values(context, "Series_overall_design"))
            if designs:
                result["OveralDesign"] = "\n\n".join(dict.fromkeys(designs))

        return result, diagnostics

    def _extract_legacy(self, data: dict[str, Any]) -> dict[str, str]:
        flat: dict[str, str] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    flat[_normalize_key(k)] = v.strip()
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and ":" in item:
                            ik, _, iv = item.partition(":")
                            flat[_normalize_key(ik)] = iv.strip()
                        elif isinstance(item, str):
                            flat[_normalize_key(f"{k} {item}")] = item.strip()
                elif isinstance(v, dict):
                    for sk, sv in v.items():
                        if isinstance(sv, str):
                            flat[_normalize_key(f"{k} {sk}")] = sv.strip()

            char_list = data.get("characteristics", [])
            if isinstance(char_list, list):
                for item in char_list:
                    if isinstance(item, str) and ":" in item:
                        k, _, v = item.partition(":")
                        flat[_normalize_key(k)] = v.strip()
                    elif isinstance(item, dict):
                        for k, v in item.items():
                            if isinstance(v, str):
                                flat[_normalize_key(k)] = v.strip()

            attrs = data.get("_attributes", {})
            if isinstance(attrs, dict):
                for k, v in attrs.items():
                    if isinstance(v, str):
                        flat[_normalize_key(k)] = v.strip()

        result: dict[str, str] = {}
        for rule in self._rules:
            target = rule.get("target_column")
            if not target:
                continue

            match_key = rule.get("match_key_regex", "")
            value_map = rule.get("value_map", {})
            direct_key = rule.get("direct_key")
            default = rule.get("default")
            source_keys = rule.get("source_keys", [])

            matched_value: str | None = None

            if direct_key:
                nk = _normalize_key(direct_key)
                if nk in flat:
                    matched_value = flat[nk]
            elif source_keys:
                matched_value = next(
                    (flat[_normalize_key(key)] for key in source_keys if _normalize_key(key) in flat), None
                )
            elif match_key:
                pattern = re.compile(match_key, re.IGNORECASE)
                for k, v in flat.items():
                    if pattern.search(k) or pattern.search(v):
                        matched_value = v
                        break

            if matched_value is not None and value_map:
                matched_value = _apply_value_map(matched_value, [{"value_map": value_map}])

            if matched_value is not None:
                result[target] = matched_value
            elif default is not None and target not in result:
                result[target] = default

        return result


def _normalize_key(key: str) -> str:
    return re.sub(r"[\s_\-]+", " ", key.strip().lower())
