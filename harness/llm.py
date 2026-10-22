"""Single OpenAI-compatible client and shared token budget accounting."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, urlopen


class BudgetExceeded(RuntimeError):
    """Raised before a request would exceed configured token allowances."""


@dataclass
class Usage:
    agents: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def add(self, agent: str, tokens: int) -> None:
        self.agents[agent] = self.agents.get(agent, 0) + tokens
        self.total += tokens


class LLMClient:
    def __init__(self, config: dict[str, Any], *, stub: bool = False):
        self.config = config
        self.stub = stub
        self.usage = Usage()
        self.warnings: list[str] = []

    def account(self, agent: str, tokens: int) -> None:
        budget = self.config["budget"]
        agent_limit = int(budget["per_agent"][agent])
        total_limit = int(budget["total_tokens"])
        self.usage.add(agent, tokens)
        if self.usage.agents[agent] >= agent_limit * budget["warn_at"]:
            self.warnings.append(f"{agent} reached its token warning threshold")
        if self.usage.total >= total_limit * budget["warn_at"]:
            self.warnings.append("total token warning threshold reached")
        abort_at = float(budget["abort_at"])
        if self.usage.agents[agent] >= agent_limit * abort_at:
            raise BudgetExceeded(f"{agent} reached its token abort threshold")
        if self.usage.total >= total_limit * abort_at:
            raise BudgetExceeded("total token abort threshold reached")

    def complete(self, agent: str, messages: list[dict[str, str]], *, json_mode: bool = True) -> tuple[str, int, float]:
        if self.stub:
            raise RuntimeError("stub mode does not make LLM requests")
        model_cfg = self.config["models"][agent]
        provider = model_cfg["provider"]
        base_url = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("set OPENAI_API_KEY or OPENROUTER_API_KEY to use live models")
        payload: dict[str, Any] = {"model": model_cfg["name"], "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "HTTP-Referer": "https://localhost", "X-Title": "Prompt Execute Verify Harness"},
            method="POST",
        )
        started = time.monotonic()
        with urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
        latency = time.monotonic() - started
        choice = result["choices"][0]["message"]["content"]
        used = int(result.get("usage", {}).get("total_tokens", 0))
        self.account(agent, used)
        return choice, used, latency
