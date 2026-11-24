from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

# Bump when deterministic extraction behavior changes, independently of profile
# data. Older canonical rows must be revisited after an implementation fix.
EXTRACTION_VERSION = "2026-10-22-source-evidence-v1"


def compute_profile_fingerprint(profile_dir: Path) -> str:
    h = hashlib.sha256()
    h.update(EXTRACTION_VERSION.encode())

    schema_path = profile_dir / "schema.yaml"
    if schema_path.exists():
        h.update(schema_path.read_bytes())

    vocab_dir = profile_dir / "vocab"
    if vocab_dir.is_dir():
        for vf in sorted(vocab_dir.glob("*.csv")):
            h.update(vf.name.encode())
            h.update(vf.read_bytes())

    rules_path = profile_dir / "rules.yaml"
    if rules_path.exists():
        h.update(rules_path.read_bytes())

    prompt_path = profile_dir / "prompt.md"
    if prompt_path.exists():
        h.update(prompt_path.read_bytes())

    return h.hexdigest()


def compute_prompt_fingerprint(profile_dir: Path, model_id: str) -> str:
    h = hashlib.sha256()
    prompt_path = profile_dir / "prompt.md"
    if prompt_path.exists():
        h.update(prompt_path.read_bytes())
    schema_path = profile_dir / "schema.yaml"
    if schema_path.exists():
        schema = yaml.safe_load(schema_path.read_text()) or {}
        # Include the semantic tool contract, not vocab CSV contents: vocab-only
        # normalization updates should still reuse model answers.
        h.update(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode())
    h.update(model_id.encode())
    return h.hexdigest()


def with_llm_transport(fingerprint: str, cache_identity: str) -> str:
    """Separate extraction/cache state by model, protocol and API destination."""
    return hashlib.sha256(f"{fingerprint}\nllm-transport-v1\n{cache_identity}".encode()).hexdigest()
