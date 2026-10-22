"""Persisted state machine for the prompt, execute, verify workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from harness.agents.common import write_json
from harness.agents.pe import PromptEngineer
from harness.agents.ex import Executor
from harness.agents.vr import Verifier
from harness.agents.mc import MemoryCurator
from harness.events import EventLog
from harness.llm import LLMClient, BudgetExceeded
from harness.memory import MemoryStore
from harness.metrics import fuzzy_issue_match, preflight, score_table
from harness.schemas import ImprovedPrompt, Verdict


STATES = (
    "INIT", "CONTEXT", "PROMPTING", "GATE1", "EXECUTING", "PREFLIGHT",
    "VERIFYING", "REVISING", "GATE2", "CURATING", "DONE", "ESCALATED", "ABORTED",
)
TERMINAL = {"DONE", "ESCALATED", "ABORTED"}


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

    def run(self, request: str, *, gate1: Callable[[dict], Any] | None = None,
            gate2: Callable[[dict], Any] | None = None) -> dict:
        state = {
            "run_id": self.run_id, "request": request, "state": "INIT", "round": 0,
            "transitions": ["INIT"], "rounds": [], "usage": {"agents": {}, "total": 0}, "pe_rewrites": 0,
            "revision_summaries": [], "gate1_decisions": [], "gate2_decision": None,
            "best_draft": None, "best_score": -1.0,
        }
        self.events.write("INIT", "controller", None, {}, 0.0)
        self._checkpoint(state)
        return self._advance(state, gate1=gate1, gate2=gate2)

    def resume(self, *, gate1: Callable[[dict], Any] | None = None,
               gate2: Callable[[dict], Any] | None = None) -> dict:
        if not self.checkpoint_path.exists():
            raise FileNotFoundError(f"no checkpoint for run {self.run_id}")
        state = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        usage = state.get("usage", {})
        self.client.usage.agents.update(usage.get("agents", {}))
        self.client.usage.total = usage.get("total", 0)
        return self._advance(state, gate1=gate1, gate2=gate2)

    def _checkpoint(self, state: dict) -> None:
        state["usage"] = {"agents": dict(self.client.usage.agents), "total": self.client.usage.total}
        write_json(self.checkpoint_path, state)

    def _transition(self, state: dict, target: str, *, detail: dict | None = None) -> None:
        if target not in STATES:
            raise ValueError(f"unknown controller state: {target}")
        state["state"] = target
        state.setdefault("transitions", []).append(target)
        self.events.write(target, "controller", None, {}, 0.0, **(detail or {}))
        self._checkpoint(state)

    def _escalate(self, state: dict, reason: str) -> None:
        state["escalation_reason"] = reason
        self._transition(state, "ESCALATED", detail={"reason": reason, "best_draft": state.get("best_draft")})

    @staticmethod
    def _prompt_payload(prompt: ImprovedPrompt) -> dict:
        return prompt.model_dump(mode="json")

    def _gate1(self, prompt: ImprovedPrompt, callback: Callable[[dict], Any] | None) -> tuple[str, ImprovedPrompt | None, str]:
        payload = self._prompt_payload(prompt)
        if callback is None:
            return "approve", prompt, ""
        result = callback(payload)
        if isinstance(result, str):
            decision, separator, reason = result.strip().partition(":")
            decision = decision.strip().lower()
            if decision == "edit":
                raise ValueError("Gate 1 edit requires a complete prompt mapping")
            return decision, prompt, reason.strip() if separator else ""
        if not isinstance(result, dict):
            raise ValueError("Gate 1 must return approve, reject, or an edit mapping")
        decision = str(result.get("decision", "edit")).strip().lower()
        reason = str(result.get("reason", ""))
        if decision == "edit":
            edited = result.get("prompt", {k: v for k, v in result.items() if k not in {"decision", "reason"}})
            return decision, ImprovedPrompt.model_validate(edited), reason
        return decision, prompt, reason

    @staticmethod
    def _gate2(callback: Callable[[dict], Any] | None, payload: dict) -> tuple[str, str]:
        if callback is None:
            return "accept", ""
        result = callback(payload)
        if result is None or result == "":
            return "no_answer", ""
        if isinstance(result, str):
            decision, _, reason = result.strip().partition(":")
            return decision.strip().lower(), reason.strip()
        return str(result.get("decision", "no_answer")).strip().lower(), str(result.get("reason", ""))

    def _preflight_issue(self, prompt: dict, draft: str) -> dict | None:
        return preflight(prompt, draft)

    def _save_draft(self, round_no: int, draft: str) -> None:
        write_json(self.run_dir / "drafts" / f"round-{round_no}.json", {"round": round_no, "draft": draft})

    def _advance(self, state: dict, *, gate1: Callable[[dict], Any] | None,
                 gate2: Callable[[dict], Any] | None) -> dict:
        try:
            while state["state"] not in TERMINAL:
                current = state["state"]
                if current == "INIT":
                    self._transition(state, "CONTEXT")
                elif current == "CONTEXT":
                    state["context_pack"] = self.memory.search(state["request"], top_k=5)
                    self._transition(state, "PROMPTING")
                elif current == "PROMPTING":
                    if state["pe_rewrites"] >= int(self.config["limits"]["pe_rewrites"]):
                        self._escalate(state, "PE rewrite limit reached")
                        continue
                    feedback = state.pop("prompt_feedback", "")
                    request = state["request"] + (f"\n\nController feedback: {feedback}" if feedback else "")
                    prompt = self.agents["pe"].run_stub(request)
                    state["pe_rewrites"] += 1
                    state["improved_prompt"] = self._prompt_payload(ImprovedPrompt.model_validate(prompt))
                    self._transition(state, "GATE1")
                elif current == "GATE1":
                    if gate1 is None:
                        self.write_report(state)
                        return state
                    decision, prompt, reason = self._gate1(ImprovedPrompt.model_validate(state["improved_prompt"]), gate1)
                    state["gate1_decisions"].append({"decision": decision, "reason": reason})
                    if decision == "edit":
                        state["improved_prompt"] = self._prompt_payload(prompt)
                        decision = "approve"
                    if decision in {"approve", "accept", "yes"}:
                        state["gate1_approved"] = True
                        self._transition(state, "EXECUTING", detail={"gate1": "approve"})
                    elif decision == "reject":
                        state["gate1_approved"] = False
                        if not reason.strip():
                            raise ValueError("Gate 1 reject requires a reason")
                        state["prompt_feedback"] = reason
                        if state["pe_rewrites"] >= int(self.config["limits"]["pe_rewrites"]):
                            self._escalate(state, "PE rewrite limit reached after Gate 1 rejection")
                        else:
                            self._transition(state, "PROMPTING", detail={"gate1": "reject", "reason": reason})
                    else:
                        raise ValueError("Gate 1 decision must be approve, edit, or reject")
                elif current == "EXECUTING":
                    if not state.get("gate1_approved"):
                        raise RuntimeError("execution requires Gate 1 approval")
                    if len(state["rounds"]) >= int(self.config["limits"]["rounds"]):
                        self._escalate(state, "EX/VR round limit reached")
                        continue
                    state["round"] += 1
                    round_no = state["round"]
                    issues = state.pop("revision_issues", None)
                    previous = (state.get("best_draft") or state.get("draft")) if issues else None
                    if self.stub:
                        draft = self.agents["ex"].run_stub(state["improved_prompt"], issues=issues, previous_draft=previous)
                    else:
                        draft = self.agents["ex"].run(self.client, state["improved_prompt"], issues=issues,
                                                      previous_draft=previous)
                    state["draft"] = draft
                    self._save_draft(round_no, draft)
                    self._transition(state, "PREFLIGHT")
                elif current == "PREFLIGHT":
                    issue = self._preflight_issue(state["improved_prompt"], state["draft"])
                    if issue:
                        state["pending_preflight_issue"] = issue
                        state["rounds"].append({"round": state["round"], "decision": "PREFLIGHT", "total": 0.0})
                        state["revision_issues"] = [issue]
                        state["revision_summaries"].append({"round": state["round"], "decision": "PREFLIGHT", "total": 0.0})
                        self._transition(state, "REVISING", detail={"issue": issue})
                    else:
                        self._transition(state, "VERIFYING")
                elif current == "VERIFYING":
                    verdict = Verdict.model_validate(self.agents["vr"].run_stub(state["draft"]))
                    round_no = state["round"]
                    row = {"round": round_no, "decision": verdict.decision, "total": verdict.total}
                    state["rounds"].append(row)
                    self.events.write("VERDICT", "vr", self.agents["vr"].version, {}, 0.0,
                                      decision=verdict.decision, total=verdict.total)
                    write_json(self.run_dir / "verdicts" / f"round-{round_no}.json", verdict.model_dump(mode="json"))
                    if verdict.total > state.get("best_score", -1.0):
                        state["best_score"], state["best_draft"] = verdict.total, state["draft"]
                    previous_issues = state.get("last_verdict_issues", [])
                    current_issues = [i.model_dump() for i in verdict.issues]
                    if verdict.decision == "REVISE" and self._repeated_issue(previous_issues, current_issues):
                        self._escalate(state, "same metric and quoted span recurred in consecutive Verdicts")
                        continue
                    state["last_verdict_issues"] = current_issues
                    prior_scores = [r["total"] for r in state["rounds"] if r["decision"] != "PREFLIGHT"]
                    delta = prior_scores[-1] - prior_scores[-2] if len(prior_scores) > 1 else None
                    stalls = state.get("stall_count", 0)
                    stalls = stalls + 1 if delta is not None and delta < float(self.config["thresholds"]["stall_delta"]) else 0
                    state["stall_count"] = stalls
                    summary = {**row, "issues": [i.model_dump() for i in verdict.issues]}
                    state["revision_summaries"].append(summary)
                    if stalls >= 2:
                        self._escalate(state, "score improved by less than stall_delta for two consecutive rounds")
                    elif verdict.decision == "NEEDS_HUMAN":
                        self._escalate(state, "verifier requested human review")
                    elif verdict.decision == "PROMPT_PROBLEM":
                        if not verdict.prompt_diagnosis.strip():
                            self._escalate(state, "PROMPT_PROBLEM had no diagnosis")
                        else:
                            state["prompt_feedback"] = verdict.prompt_diagnosis
                            state["gate1_approved"] = False
                            if state["pe_rewrites"] >= int(self.config["limits"]["pe_rewrites"]):
                                self._escalate(state, "PE rewrite limit reached after PROMPT_PROBLEM")
                            else:
                                self._transition(state, "PROMPTING", detail={"decision": "PROMPT_PROBLEM"})
                    elif verdict.decision == "PASS":
                        band_ok = verdict.length.get("status") == "OK"
                        factual = verdict.scores.get("factual accuracy", verdict.scores.get("factuality", 1.0))
                        no_blocker = not any(i.severity == "blocker" for i in verdict.issues)
                        if verdict.total >= float(self.config["thresholds"]["pass_total"]) and factual >= float(self.config["thresholds"]["factual_floor"]) and band_ok and no_blocker:
                            state["verdict"] = verdict.model_dump(mode="json")
                            self._transition(state, "GATE2")
                        else:
                            state["revision_issues"] = [i.model_dump() for i in verdict.issues] or [{
                                "metric": "pass_threshold", "severity": "major", "quote": state["draft"][:120],
                                "fix": "Meet total, factual accuracy, and in-band length thresholds."}]
                            self._transition(state, "REVISING", detail={"decision": "PASS_FAILED_HARD_GATE"})
                    else:
                        state["revision_issues"] = [i.model_dump() for i in verdict.issues]
                        self._transition(state, "REVISING", detail={"decision": verdict.decision})
                elif current == "REVISING":
                    state["gate1_approved"] = True
                    if len(state["rounds"]) >= int(self.config["limits"]["rounds"]):
                        self._escalate(state, "EX/VR round limit reached")
                    else:
                        self._transition(state, "EXECUTING")
                elif current == "GATE2":
                    if gate2 is None:
                        self.write_report(state)
                        return state
                    decision, reason = self._gate2(gate2, {"draft": state["draft"], "best_draft": state["best_draft"],
                                                           "verdict": state["verdict"]})
                    if decision not in {"accept", "reject", "no_answer", "no-answer", "no answer", ""}:
                        raise ValueError("Gate 2 decision must be accept, reject, or no answer")
                    if decision == "reject" and not reason:
                        raise ValueError("Gate 2 reject requires a reason")
                    decision = "no_answer" if decision in {"no-answer", "no answer", ""} else decision
                    state["gate2_decision"] = {"decision": decision, "reason": reason}
                    self._transition(state, "CURATING")
                elif current == "CURATING":
                    decision = (state.get("gate2_decision") or {}).get("decision")
                    if decision == "accept":
                        memwrites = self.agents["mc"].run_stub(state["request"])
                        for item in memwrites[:int(self.config["mem0"]["max_writes_per_run"])]:
                            self.memory.write(item.model_dump(mode="json"), self.config["mem0"]["max_writes_per_run"])
                    elif decision == "reject":
                        self.memory.write({"text": state["gate2_decision"]["reason"], "type": "failure", "confidence": 1.0},
                                          self.config["mem0"]["max_writes_per_run"])
                    self._transition(state, "DONE")
                else:
                    raise RuntimeError(f"cannot advance from {current}")
            self.write_report(state)
            return state
        except BudgetExceeded as exc:
            self._transition(state, "ABORTED", detail={"reason": str(exc), "best_draft": state.get("best_draft")})
            self._escalate(state, str(exc))
            state["error"] = str(exc)
            self.write_report(state)
            return state
        except RuntimeError as exc:
            if not str(exc).startswith("ESCALATED:"):
                raise
            self._escalate(state, str(exc))
            state["error"] = str(exc)
            self.write_report(state)
            return state

    def _repeated_issue(self, previous: list[dict], current: list[dict]) -> bool:
        threshold = float(self.config["thresholds"]["fuzzy_match"])
        return fuzzy_issue_match(previous, current, threshold)

    def write_report(self, state: dict | None = None) -> None:
        state = state or json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        agents = self.client.usage.agents or state.get("usage", {}).get("agents", {})
        total_tokens = self.client.usage.total or state.get("usage", {}).get("total", 0)
        report = {
            "run_id": self.run_id, "status": state.get("state", "ESCALATED"),
            "gate_1": state.get("gate1_decisions", []), "gate_2": state.get("gate2_decision") or "PENDING",
            "rounds": state.get("rounds", []), "best_draft": state.get("best_draft"),
            "per_agent_tokens": {key: agents.get(key, 0) for key in ("pe", "ex", "vr", "mc")},
            "total_tokens": total_tokens, "headroom_tokens_saved": 0,
            "mem0": {"hits": self.memory.hits, "misses": self.memory.misses, "writes": len(self.memory.writes)},
            "escalation_reason": state.get("escalation_reason"),
        }
        write_json(self.run_dir / "report.json", report)
        table = score_table(state.get("rounds", []))
        (self.run_dir / "report.md").write_text(
            f"# Run {self.run_id}\n\nFinal status: **{report['status']}**\n\nGate 1 decisions: {report['gate_1']}\n\nGate 2: {report['gate_2']}\n\n## Per-round scores\n\n{table}\n\n## Tokens\n\n" +
            "\n".join(f"- {agent}: {count}" for agent, count in report["per_agent_tokens"].items()) +
            f"\n- Total: {report['total_tokens']}\n- Headroom tokens saved: {report['headroom_tokens_saved']}\n\nMem0 hits/misses/writes: {self.memory.hits}/{self.memory.misses}/{len(self.memory.writes)}\n",
            encoding="utf-8",
        )
