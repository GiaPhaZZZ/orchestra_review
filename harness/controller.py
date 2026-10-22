"""T1 orchestration seam: resumable deterministic stub execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.agents.common import write_json
from harness.agents.pe import PromptEngineer
from harness.agents.ex import Executor
from harness.agents.vr import Verifier
from harness.agents.mc import MemoryCurator
from harness.events import EventLog
from harness.llm import LLMClient, BudgetExceeded
from harness.memory import MemoryStore
from harness.metrics import score_table


class Controller:
    def __init__(self, config: dict[str, Any], *, repo_root: Path, run_id: str, stub: bool = True):
        self.config, self.root, self.run_id, self.stub = config, repo_root, run_id, stub
        self.run_dir = repo_root / "runs" / run_id
        for folder in ("drafts", "verdicts"):
            (self.run_dir / folder).mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.run_dir / "events.jsonl", run_id)
        prompt_dir = repo_root / config["prompts"]["dir"]
        self.agents = {
            "pe": PromptEngineer(prompt_dir / "pe.md"),
            "ex": Executor(prompt_dir / "ex.md"),
            "vr": Verifier(prompt_dir / "vr.md"),
            "mc": MemoryCurator(prompt_dir / "mc.md"),
        }
        self.client = LLMClient(config, stub=stub)
        self.memory = MemoryStore()

    @property
    def checkpoint_path(self) -> Path:
        return self.run_dir / "checkpoint.json"

    def run(self, request: str) -> dict:
        state: dict[str, Any] = {"run_id": self.run_id, "request": request, "phase": "start", "round": 0, "rounds": [], "usage": {"agents": {}, "total": 0}}
        return self._advance(state)

    def resume(self) -> dict:
        if not self.checkpoint_path.exists():
            raise FileNotFoundError(f"no checkpoint for run {self.run_id}")
        state = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        self.client.usage.agents.update(state.get("usage", {}).get("agents", {}))
        self.client.usage.total = state.get("usage", {}).get("total", 0)
        return self._advance(state)

    def _checkpoint(self, state: dict) -> None:
        state["usage"] = {"agents": self.client.usage.agents, "total": self.client.usage.total}
        write_json(self.checkpoint_path, state)

    def _advance(self, state: dict) -> dict:
        try:
            if state["phase"] == "start":
                self.events.write("GATE_1", "pe", self.agents["pe"].version, {"pe": 0}, 0.0)
                improved = self.agents["pe"].run_stub(state["request"])
                state["improved_prompt"] = improved.model_dump(mode="json")
                state["phase"] = "prompt_ready"
                self._checkpoint(state)
            if state["phase"] == "prompt_ready":
                draft = self.agents["ex"].run_stub(state["improved_prompt"])
                state["draft"] = draft
                state["phase"] = "draft_ready"
                self.events.write("EXECUTE", "ex", self.agents["ex"].version, {"ex": 0}, 0.0)
                write_json(self.run_dir / "drafts" / "round-1.json", {"round": 1, "draft": draft})
                self._checkpoint(state)
            if state["phase"] == "draft_ready":
                verdict = self.agents["vr"].run_stub(state["draft"])
                state["verdict"] = verdict.model_dump(mode="json")
                state["rounds"].append({"round": 1, "decision": verdict.decision, "total": verdict.total})
                state["phase"] = "verified"
                self.events.write("GATE_2", "vr", self.agents["vr"].version, {"vr": 0}, 0.0, decision=verdict.decision, total=verdict.total)
                write_json(self.run_dir / "verdicts" / "round-1.json", state["verdict"])
                self._checkpoint(state)
            if state["phase"] == "verified":
                memwrites = self.agents["mc"].run_stub(state["request"])
                for item in memwrites:
                    self.memory.write(item.model_dump(mode="json"), self.config["mem0"]["max_writes_per_run"])
                state["phase"] = "DONE" if state["verdict"]["decision"] == "PASS" else "ESCALATED"
                state["memory_writes"] = len(self.memory.writes)
                self.events.write("COMPLETE", "mc", self.agents["mc"].version, {"mc": 0}, 0.0, status=state["phase"])
                self._checkpoint(state)
            self.write_report(state)
            return state
        except BudgetExceeded as exc:
            state["phase"] = "ABORTED"
            state["error"] = str(exc)
            self._checkpoint(state)
            self.write_report(state)
            return state
        except RuntimeError as exc:
            if not str(exc).startswith("ESCALATED:"):
                raise
            state["phase"] = "ESCALATED"
            state["error"] = str(exc)
            self.events.write("ESCALATED", "controller", None, {}, 0.0, reason=str(exc))
            self._checkpoint(state)
            self.write_report(state)
            return state

    def write_report(self, state: dict | None = None) -> None:
        state = state or json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        verdict = state.get("verdict", {})
        table = score_table(state.get("rounds", []))
        agents = self.client.usage.agents or state.get("usage", {}).get("agents", {})
        total_tokens = self.client.usage.total or state.get("usage", {}).get("total", 0)
        report = {
            "run_id": self.run_id,
            "status": state.get("phase", "ESCALATED"),
            "gate_1": "PASS" if state.get("improved_prompt") else "PENDING",
            "gate_2": verdict.get("decision", "PENDING"),
            "rounds": state.get("rounds", []),
            "per_agent_tokens": {key: agents.get(key, 0) for key in ("pe", "ex", "vr", "mc")},
            "total_tokens": total_tokens,
            "headroom_tokens_saved": 0,
            "mem0": {"hits": self.memory.hits, "misses": self.memory.misses, "writes": len(self.memory.writes)},
        }
        write_json(self.run_dir / "report.json", report)
        (self.run_dir / "report.md").write_text(
            f"# Run {self.run_id}\n\nFinal status: **{report['status']}**\n\nGate 1: {report['gate_1']}\n\nGate 2: {report['gate_2']}\n\n## Per-round scores\n\n{table}\n\n## Tokens\n\n" +
            "\n".join(f"- {agent}: {count}" for agent, count in report["per_agent_tokens"].items()) +
            f"\n- Total: {report['total_tokens']}\n- Headroom tokens saved: {report['headroom_tokens_saved']}\n\nMem0 hits/misses/writes: {self.memory.hits}/{self.memory.misses}/{len(self.memory.writes)}\n",
            encoding="utf-8",
        )
