"""Optional Mem0 adapter with scoped reads and ADD-only, curated writes."""

from __future__ import annotations

import re
from typing import Any

READ_TYPES = {
    "pe": {"preference", "prompt_pattern"},
    "vr": {"failure", "domain_fact"},
}
ALLOWED_TYPES = {"preference", "prompt_pattern", "failure", "domain_fact"}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_ -]?key|token|credential)\s*[:=]\s*\S+"),
)


class Mem0Store:
    """Wrap a Mem0 client; pass a client for tests or lazy-load Mem0 on demand."""

    def __init__(self, config: dict[str, Any] | None = None, *, client: Any = None):
        self.config = config or {}
        options = self.config.get("mem0", self.config)
        self.user_id = options.get("user_id", "astral")
        self.top_k = int(options.get("top_k", 5))
        self.max_writes = min(5, int(options.get("max_writes_per_run", 5)))
        self.client = client
        self.hits = self.misses = self.write_count = 0
        self.writes: list[dict[str, Any]] = []

    def _client(self):
        if self.client is None:
            try:
                from mem0 import Memory
            except ImportError as exc:
                raise RuntimeError("Mem0 is optional; install mem0ai[nlp] to enable it") from exc
            self.client = Memory.from_config(self.config.get("mem0", {}).get("config", {}))
        return self.client

    @staticmethod
    def _results(response: Any) -> list[dict[str, Any]]:
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            results = response.get("results", [])
            return results if isinstance(results, list) else []
        return []

    def search(self, query: str, *, agent: str = "pe", top_k: int | None = None) -> list[dict[str, Any]]:
        if agent not in READ_TYPES:
            raise ValueError("memory reads are supported only for pe and vr")
        limit = min(top_k or self.top_k, 5)
        client = self._client()
        filters = {"AND": [{"user_id": self.user_id}, {"metadata.type": {"in": sorted(READ_TYPES[agent])}}]}
        try:
            response = client.search(query, user_id=self.user_id, limit=limit, filters=filters)
            results = self._results(response)
        except Exception:
            # Mem0 filter syntax differs by release. Bound fallback reads and filter locally.
            response = client.search(query, user_id=self.user_id, limit=15)
            results = self._results(response)
        # Enforce the agent boundary locally even if a Mem0 release ignores filters.
        results = [item for item in results if (item.get("metadata") or {}).get("type") in READ_TYPES[agent]]
        self.hits += len(results)
        self.misses += int(not results)
        return results[:limit]

    @staticmethod
    def _safe_text(text: str) -> bool:
        normalized = text.strip()
        if not normalized or len(normalized) > 1000:
            return False
        lowered = normalized.lower()
        if "```" in normalized or "<draft>" in lowered or "full draft" in lowered:
            return False
        return not any(pattern.search(normalized) for pattern in SECRET_PATTERNS)

    def write(self, item: dict[str, Any], *, run_id: str, agent_id: str = "mc",
              gate2: str, accepted: bool = False, verified: bool = False) -> bool:
        """Add one eligible memory after Gate 2; never update or overwrite existing items."""
        kind, text = item.get("type"), str(item.get("text", ""))
        if agent_id != "mc" or gate2 not in {"accept", "reject"}:
            return False
        if self.write_count >= self.max_writes or kind not in ALLOWED_TYPES or not self._safe_text(text):
            return False
        if gate2 == "reject":
            if kind != "failure" or accepted:
                return False
        elif not accepted or (kind == "domain_fact" and not verified) or kind == "failure":
            return False
        metadata = {"type": kind, "run_id": run_id, "agent_id": agent_id}
        # Search before each add; duplicates are scoped to this user and remain untouched.
        prior = self._results(self._client().search(text, user_id=self.user_id, limit=5))
        if any(self._near_duplicate(text, str(hit.get("memory", hit.get("text", "")))) for hit in prior):
            return False
        payload = {"text": text.strip(), "type": kind, "confidence": item.get("confidence", 1.0), "metadata": metadata}
        self._client().add(payload["text"], user_id=self.user_id, agent_id=agent_id, metadata=metadata)
        self.write_count += 1
        self.writes.append(payload)
        return True

    @staticmethod
    def _near_duplicate(left: str, right: str) -> bool:
        tokens_a, tokens_b = set(re.findall(r"\w+", left.lower())), set(re.findall(r"\w+", right.lower()))
        return bool(tokens_a and tokens_b and len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= 0.8)


class MemoryStore(Mem0Store):
    """Backward-compatible in-memory store used by the T1 stub controller."""

    def __init__(self):
        super().__init__({"mem0": {"user_id": "astral", "top_k": 5, "max_writes_per_run": 5}})
        self.client = _InMemoryMem0()

    def write(self, item: dict[str, Any], max_writes: int = 5) -> bool:
        """Retain the T1 stub controller API; production writes use Mem0Store.write."""
        if len(self.writes) >= min(5, max_writes):
            return False
        self.writes.append(item)
        return True


class _InMemoryMem0:
    def __init__(self):
        self.items: list[dict[str, Any]] = []

    def search(self, query: str, *, user_id: str, limit: int = 5, filters: Any = None):
        items = [item for item in self.items if item["user_id"] == user_id]
        if filters:
            allowed = set(filters["AND"][1]["metadata.type"]["in"])
            items = [item for item in items if item["metadata"].get("type") in allowed]
        return {"results": items[:limit]}

    def add(self, text: str, *, user_id: str, agent_id: str, metadata: dict[str, Any]):
        self.items.append({"memory": text, "user_id": user_id, "agent_id": agent_id, "metadata": metadata})
