"""Append-only JSONL run event log."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, state: str, agent: str, prompt_version: int | None, token_counts: dict[str, int], latency: float, **extra: Any) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "state": state,
            "agent": agent,
            "prompt_version": prompt_version,
            "token_counts": token_counts,
            "latency": round(latency, 6),
            **extra,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
