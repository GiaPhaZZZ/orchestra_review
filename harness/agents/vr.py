"""Prompt-independent verifier input, claim checking, and score gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from harness.agents.common import prompt_version, strict_json
from harness.metrics import concision_score, length_status
from harness.schemas import ClaimCheck, ImprovedPrompt, Issue, Verdict


WEIGHTS = {
    "instruction compliance": 0.25,
    "factual accuracy": 0.25,
    "completeness": 0.15,
    "clarity and structure": 0.10,
    "style, tone, audience fit": 0.10,
    "concision": 0.15,
}


class Verifier:
    key = "vr"

    def __init__(self, prompt_path: Path, *, search_enabled: bool = False,
                 search_tool: Callable[[str], str] | None = None):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)
        self.search_enabled = search_enabled
        self.search_tool = search_tool

    @staticmethod
    def build_input(improved_prompt: dict, sources: list[str], draft: str,
                    earlier_verdict_summary: str = "") -> dict:
        """Build VR's entire input from the five allowed fields only."""
        prompt = ImprovedPrompt.model_validate(improved_prompt)
        summary = " ".join(earlier_verdict_summary.splitlines()).strip()
        return {
            "approved_prompt": prompt.costar.model_dump(),
            "success_criteria": list(prompt.success_criteria),
            "sources": list(sources),
            "current_draft": draft,
            "earlier_verdict_summary": summary,
        }

    def run(self, client, improved_prompt: dict, draft: str, *, sources: list[str] | None = None,
            earlier_verdict_summary: str = "", non_factual: bool = False) -> Verdict:
        prompt = ImprovedPrompt.model_validate(improved_prompt)
        source_list = list(sources or [])
        if not source_list and self.search_enabled and self.search_tool:
            query = prompt.costar.objective.strip()
            result = self.search_tool(query)
            source_list = [str(result)] if result else []
        payload = self.build_input(improved_prompt, source_list, draft, earlier_verdict_summary)
        system = self.prompt_path.read_text(encoding="utf-8")
        raw, _, _ = strict_json(client, self.key, Verdict, [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ])
        return self.enforce(raw, prompt.model_dump(mode="json"), draft, has_sources=bool(source_list),
                            non_factual=non_factual)

    def run_stub(self, draft: str, improved_prompt: dict | None = None, *, sources: list[str] | None = None,
                 earlier_verdict_summary: str = "", non_factual: bool = False) -> Verdict:
        """Deterministic clean-case stub; realistic defect checks are in focused tests."""
        if improved_prompt is None:
            band = {"min": 1, "max": 500, "unit": "words"}
            criteria = ["Include a direct answer", "Exclude unsupported claims", "Return a readable response"]
            costar = {"context": "", "objective": "", "style": "", "tone": "", "audience": "", "response": ""}
            improved_prompt = {"costar": costar, "length_band": band, "success_criteria": criteria,
                               "assumptions": [], "changes_summary": ""}
        prompt = ImprovedPrompt.model_validate(improved_prompt)
        length = length_status(draft, prompt.length_band.model_dump())
        claims = []
        factual = 1.0
        if not sources and not non_factual:
            factual = 0.0
        scores = {name: 1.0 for name in WEIGHTS}
        scores["factual accuracy"] = factual
        scores["concision"] = concision_score(draft, prompt.length_band.model_dump())
        if length["status"] != "OK":
            scores["instruction compliance"] = 0.0
        if not sources and not non_factual:
            scores["instruction compliance"] = min(scores["instruction compliance"], 0.79)
        proposed = Verdict(decision="PASS", scores=scores, total=0.0, issues=[], claims=claims,
                           length=length)
        return self.enforce(proposed, prompt.model_dump(mode="json"), draft,
                            has_sources=bool(sources), non_factual=non_factual)

    @staticmethod
    def enforce(verdict: Verdict, improved_prompt: dict, draft: str, *, has_sources: bool,
                non_factual: bool = False) -> Verdict:
        """Recompute weighted score and apply v2.0 decision rules deterministically."""
        prompt = ImprovedPrompt.model_validate(improved_prompt)
        scores = {name: float(verdict.scores.get(name, 0.0)) for name in WEIGHTS}
        checks = [claim if isinstance(claim, ClaimCheck) else ClaimCheck.model_validate(claim)
                  for claim in verdict.claims]
        issues = [issue if isinstance(issue, Issue) else Issue.model_validate(issue) for issue in verdict.issues]
        for claim in checks:
            if claim.status == "contradicted":
                quote = claim.claim if claim.claim in draft else draft[:min(120, len(draft))]
                if not any(i.metric == "factual accuracy" and i.quote == quote for i in issues):
                    issues.append(Issue(metric="factual accuracy", severity="blocker", quote=quote,
                                        fix="Correct or remove this claim to match the cited evidence."))
        if checks:
            scores["factual accuracy"] = sum(c.status == "supported" for c in checks) / len(checks)
        elif not has_sources and not non_factual:
            scores["factual accuracy"] = 0.0
        for claim in checks:
            if claim.status == "unverified":
                quote = claim.claim if claim.claim in draft else draft[:min(120, len(draft))]
                if not any(i.metric == "factual accuracy" and i.quote == quote for i in issues):
                    issues.append(Issue(metric="factual accuracy", severity="major", quote=quote,
                                        fix="Verify this claim against an authoritative source or remove it."))
        length = length_status(draft, prompt.length_band.model_dump())
        scores["concision"] = concision_score(draft, prompt.length_band.model_dump())
        if length["status"] != "OK":
            quote = draft[:min(120, len(draft))]
            if not any(i.metric == "length" for i in issues):
                issues.append(Issue(metric="length", severity="major", quote=quote,
                                    fix=f"Adjust the deliverable to {prompt.length_band.min}–{prompt.length_band.max} {prompt.length_band.unit}."))
        for index, issue in enumerate(issues):
            if not draft:
                raise ValueError("cannot produce a quoted issue for an empty draft")
            if not issue.quote or issue.quote not in draft or not issue.fix.strip():
                issues[index] = Issue(
                    metric=issue.metric, severity=issue.severity, quote=draft[:min(120, len(draft))],
                    fix=issue.fix.strip() or "Correct this quoted passage to meet the verifier criteria.",
                )
        total = sum(scores[name] * weight for name, weight in WEIGHTS.items())
        blocker = any(i.severity == "blocker" for i in issues)
        factual = scores["factual accuracy"]
        if not has_sources and not non_factual and checks and all(c.status == "unverified" for c in checks):
            decision = "NEEDS_HUMAN"
            if not issues:
                issues.append(Issue(metric="factual accuracy", severity="major", quote=draft[:min(120, len(draft))],
                                    fix="Provide sources or mark the Gate 1 request non_factual."))
        elif verdict.decision == "PROMPT_PROBLEM":
            decision = "PROMPT_PROBLEM"
        elif verdict.decision == "NEEDS_HUMAN":
            decision = "NEEDS_HUMAN"
        elif total >= 0.85 and not blocker and length["status"] == "OK" and factual >= 0.80:
            decision = "PASS"
        else:
            decision = "REVISE"
        if decision != "PASS" and not issues:
            failing = next((name for name, threshold in (("factual accuracy", 0.80),
                            ("instruction compliance", 0.0)) if scores[name] < threshold), "overall score")
            issues.append(Issue(metric=failing, severity="major", quote=draft[:min(120, len(draft))],
                                fix="Address the verifier's unmet criterion and improve the affected score."))
        diagnosis = verdict.prompt_diagnosis if decision == "PROMPT_PROBLEM" else ""
        return Verdict(decision=decision, scores=scores, total=total, issues=issues, claims=checks,
                       length=length, prompt_diagnosis=diagnosis)
