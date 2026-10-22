from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from harness.llm import LLMClient

T = TypeVar("T", bound=BaseModel)


def prompt_version(path: Path) -> int:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    if not first.startswith("version:"):
        raise ValueError(f"prompt must start with a version: {path}")
    return int(first.partition(":")[2].strip())


def strict_json(client: LLMClient, agent: str, model: type[T], messages: list[dict[str, str]]) -> tuple[T, int, float]:
    """Parse strict JSON and allow exactly one schema repair attempt."""
    raw, tokens, latency = client.complete(agent, messages)
    for attempt in range(2):
        try:
            return model.model_validate_json(raw), tokens, latency
        except (ValidationError, ValueError) as exc:
            if attempt:
                raise RuntimeError("ESCALATED: invalid structured output after one repair retry") from exc
            raw, retry_tokens, retry_latency = client.complete(
                agent,
                messages + [{"role": "user", "content": f"Repair your prior response. Return only valid JSON matching the schema. Validation error: {exc}"}],
            )
            tokens += retry_tokens
            latency += retry_latency
    raise AssertionError("unreachable")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
