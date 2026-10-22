import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from harness.controller import Controller
from harness.schemas import ImprovedPrompt, Verdict
from harness.llm import BudgetExceeded


ROOT = Path(__file__).resolve().parents[1]


def verdict(decision, total, quote="", metric="quality", diagnosis=""):
    issues = [] if decision == "PASS" else [{
        "metric": metric, "severity": "major", "quote": quote or f"quote {total}", "fix": "Make one concrete correction."
    }]
    return Verdict.model_validate({
        "decision": decision, "scores": {"factuality": 0.9}, "total": total,
        "issues": issues, "claims": [], "length": {"count": 2, "band": [1, 500], "status": "OK"},
        "prompt_diagnosis": diagnosis,
    })


class FakePE:
    def __init__(self):
        self.calls = []

    def run_stub(self, request):
        self.calls.append(request)
        return ImprovedPrompt.model_validate({
            "costar": {"context": "ctx", "objective": request.split("\n\nController feedback:")[0],
                       "style": "plain", "tone": "neutral", "audience": "reader", "response": "Answer"},
            "length_band": {"min": 1, "max": 500},
            "success_criteria": ["Include an answer", "Exclude invention", "Return clear prose"],
            "assumptions": [], "changes_summary": "rewritten",
        })


class FakeEX:
    def __init__(self, drafts=None):
        self.drafts = list(drafts or ["a useful answer"])
        self.calls = []

    def run_stub(self, prompt, *, issues=None, previous_draft=None):
        self.calls.append({"prompt": prompt, "issues": issues, "previous_draft": previous_draft})
        return self.drafts.pop(0) if self.drafts else previous_draft or "a useful answer"


class FakeVR:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.version = 1

    def run_stub(self, draft, improved_prompt=None, **kwargs):
        return self.verdicts.pop(0)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = yaml.safe_load((ROOT / "harness" / "config.yaml").read_text(encoding="utf-8"))
        shutil.copytree(ROOT / "harness" / "prompts", self.root / "harness" / "prompts")
        self.controller = Controller(config, repo_root=self.root, run_id="test-run", stub=True)
        self.pe, self.ex = FakePE(), FakeEX()
        self.controller.agents["pe"] = self.pe
        self.controller.agents["ex"] = self.ex

    def tearDown(self):
        self.temp.cleanup()

    def test_gate1_reject_then_validated_edit_and_gate2_no_answer(self):
        self.controller.agents["vr"] = FakeVR([verdict("PASS", 0.95)])
        calls = iter([
            {"decision": "reject", "reason": "Specify the intended output."},
            {"decision": "edit", "prompt": self.pe.run_stub("edited request").model_dump(mode="json")},
        ])
        result = self.controller.run("request", gate1=lambda _: next(calls), gate2=lambda _: None)
        self.assertEqual(result["state"], "DONE")
        self.assertEqual([d["decision"] for d in result["gate1_decisions"]], ["reject", "edit"])
        self.assertEqual(result["gate2_decision"]["decision"], "no_answer")
        self.assertEqual(self.controller.memory.writes, [])
        self.assertIn("GATE1", result["transitions"])

    def test_prompt_problem_requires_fresh_gate1(self):
        self.controller.agents["vr"] = FakeVR([
            verdict("PROMPT_PROBLEM", 0.3, diagnosis="Clarify the objective"), verdict("PASS", 0.95)
        ])
        approvals = []
        result = self.controller.run("request", gate1=lambda _: approvals.append("approved") or "approve",
                                     gate2=lambda _: "accept")
        self.assertEqual(result["state"], "DONE")
        self.assertEqual(len(approvals), 2)
        self.assertEqual(result["transitions"].count("GATE1"), 2)
        self.assertEqual(result["pe_rewrites"], 2)

    def test_gate2_rejection_records_failure_memory(self):
        self.controller.agents["vr"] = FakeVR([verdict("PASS", 0.95)])
        result = self.controller.run("request", gate1=lambda _: "approve",
                                     gate2=lambda _: {"decision": "reject", "reason": "The tone is wrong."})
        self.assertEqual(result["state"], "DONE")
        self.assertEqual(self.controller.memory.writes, [{
            "text": "The tone is wrong.", "type": "failure", "confidence": 1.0
        }])

    def test_verifier_receives_only_approved_fields_and_logs_claim_checks(self):
        class SpyVR:
            version = 2
            def __init__(inner): inner.kwargs = None
            def run_stub(inner, draft, improved_prompt, **kwargs):
                inner.kwargs = kwargs
                return Verdict.model_validate({
                    "decision": "PASS", "scores": {"factual accuracy": 1.0}, "total": 1.0,
                    "issues": [], "claims": [{"claim": "a useful answer", "status": "supported", "evidence": "source"}],
                    "length": {"count": 3, "band": [1, 500], "status": "OK"},
                })
        spy = SpyVR()
        self.controller.agents["vr"] = spy
        self.controller.run("request", gate1=lambda payload: self.assertIn("non_factual", payload) or "approve",
                            gate2=lambda _: "accept", sources=["approved source"], non_factual=False)
        self.assertEqual(set(spy.kwargs), {"sources", "earlier_verdict_summary", "non_factual"})
        self.assertEqual(spy.kwargs["sources"], ["approved source"])
        events = [json.loads(line) for line in (self.controller.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event.get("state") == "CLAIM_CHECK" and event.get("status") == "supported"
                            for event in events))

    def test_resume_reenters_checkpointed_gate(self):
        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
            self.controller.run("request", gate1=lambda _: (_ for _ in ()).throw(RuntimeError("simulated interruption")))
        checkpoint = json.loads(self.controller.checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["state"], "GATE1")
        self.controller.agents["vr"] = FakeVR([verdict("PASS", 0.95)])
        resumed = self.controller.resume(gate1=lambda _: "approve", gate2=lambda _: "accept")
        self.assertEqual(resumed["state"], "DONE")

    def test_missing_gate_callback_pauses_without_execution(self):
        paused = self.controller.run("request")
        self.assertEqual(paused["state"], "GATE1")
        self.assertEqual(paused["rounds"], [])
        self.assertIsNone(paused.get("draft"))

    def test_repeated_issue_escalates_with_best_scoring_draft(self):
        self.ex = FakeEX(["first draft", "better draft"])
        self.controller.agents["ex"] = self.ex
        self.controller.agents["vr"] = FakeVR([
            verdict("REVISE", 0.75, "the unsupported sentence"),
            verdict("REVISE", 0.84, "the unsupported sentence"),
        ])
        result = self.controller.run("request", gate1=lambda _: "approve")
        self.assertEqual(result["state"], "ESCALATED")
        self.assertEqual(result["best_draft"], "better draft")
        self.assertEqual(result["best_score"], 0.84)

    def test_lower_scoring_revision_starts_from_best_draft(self):
        self.ex = FakeEX(["best draft", "weaker draft", "final draft"])
        self.controller.agents["ex"] = self.ex
        self.controller.agents["vr"] = FakeVR([
            verdict("REVISE", 0.70, "first issue"), verdict("REVISE", 0.60, "second issue"),
            verdict("PASS", 0.95),
        ])
        result = self.controller.run("request", gate1=lambda _: "approve", gate2=lambda _: "accept")
        self.assertEqual(result["state"], "DONE")
        self.assertEqual(self.ex.calls[2]["previous_draft"], "best draft")
        self.assertEqual(result["best_draft"], "final draft")

    def test_two_small_score_gains_escalate(self):
        self.ex = FakeEX(["draft one", "draft two", "draft three"])
        self.controller.agents["ex"] = self.ex
        self.controller.agents["vr"] = FakeVR([
            verdict("REVISE", 0.50, "issue one"), verdict("REVISE", 0.51, "issue two"),
            verdict("REVISE", 0.52, "issue three"),
        ])
        result = self.controller.run("request", gate1=lambda _: "approve")
        self.assertEqual(result["state"], "ESCALATED")
        self.assertIn("stall_delta", result["escalation_reason"])
        self.assertEqual(result["best_draft"], "draft three")

    def test_budget_exhaustion_escalates(self):
        self.controller.config["budget"]["per_agent"]["ex"] = 1

        class BudgetEX(FakeEX):
            def run_stub(inner, prompt, *, issues=None, previous_draft=None):
                self.controller.client.account("ex", 1)
                return "draft"

        self.controller.agents["ex"] = BudgetEX()
        result = self.controller.run("request", gate1=lambda _: "approve")
        self.assertEqual(result["state"], "ABORTED")
        self.assertIn("abort threshold", result["escalation_reason"])
        self.assertTrue(self.controller.checkpoint_path.exists())

    def test_preflight_too_short_never_calls_verifier(self):
        class ShortPE(FakePE):
            def run_stub(inner, request):
                prompt = super(ShortPE, inner).run_stub(request).model_dump(mode="json")
                prompt["length_band"] = {"min": 3, "max": 5, "unit": "words"}
                return ImprovedPrompt.model_validate(prompt)

        self.controller.agents["pe"] = ShortPE()
        self.controller.agents["ex"] = FakeEX(["too short"] * 3)

        class NoVR:
            def run_stub(self, *args, **kwargs):
                raise AssertionError("preflight must bypass verifier")

        self.controller.agents["vr"] = NoVR()
        result = self.controller.run("request", gate1=lambda _: "approve")
        self.assertEqual(result["state"], "ESCALATED")
        self.assertEqual([row["decision"] for row in result["rounds"]], ["PREFLIGHT"] * 3)
        self.assertTrue(all(item["decision"] == "PREFLIGHT" for item in result["revision_summaries"]))

    def test_factual_issue_is_corrected_in_stub_revision(self):
        self.controller.agents["ex"] = FakeEX(["The answer is 42.", "The answer is 41."])
        self.controller.agents["vr"] = FakeVR([
            verdict("REVISE", 0.50, "The answer is 42.", metric="factual accuracy"),
            verdict("PASS", 0.95),
        ])
        result = self.controller.run("request", gate1=lambda _: "approve",
                                     gate2=lambda _: "accept", sources=["The answer is 41."])
        self.assertEqual(result["state"], "DONE")
        self.assertEqual(result["best_draft"], "The answer is 41.")
        self.assertEqual([item["decision"] for item in result["rounds"]], ["REVISE", "PASS"])

    def test_report_contains_section_11_fields_and_only_final_status(self):
        self.controller.agents["vr"] = FakeVR([verdict("PASS", 0.95)])
        self.controller.headroom.tokens_saved = 29
        self.controller.client.account("ex", 7)
        result = self.controller.run("request", gate1=lambda _: "approve", gate2=lambda _: "accept")
        report = json.loads((self.controller.run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(result["state"], "DONE")
        self.assertIn(report["status"], {"DONE", "ESCALATED", "ABORTED"})
        self.assertEqual(report["status"], "DONE")
        self.assertEqual(report["gate_1"], [{"decision": "approve", "reason": ""}])
        self.assertEqual(report["gate_2"]["decision"], "accept")
        self.assertEqual(report["rounds"], [{"round": 1, "decision": "PASS", "total": 0.95}])
        self.assertEqual(report["per_agent_tokens"], {"pe": 0, "ex": 7, "vr": 0, "mc": 0})
        self.assertEqual(report["total_tokens"], 7)
        self.assertEqual(report["headroom_tokens_saved"], 29)
        self.assertEqual(report["mem0"], {"hits": 0, "misses": 1, "writes": 0})
        markdown = (self.controller.run_dir / "report.md").read_text(encoding="utf-8")
        for text in ("Final status", "Gate 1", "Gate 2", "Per-round scores", "Headroom tokens saved", "Mem0 hits/misses/writes"):
            self.assertIn(text, markdown)

    def test_paused_checkpoint_does_not_write_final_report(self):
        paused = self.controller.run("request")
        self.assertEqual(paused["state"], "GATE1")
        self.assertFalse((self.controller.run_dir / "report.json").exists())

    def test_preflight_failure_counts_as_round_and_skips_verifier(self):
        self.ex = FakeEX(["word " * 501, "word " * 501, "word " * 501])
        self.controller.agents["ex"] = self.ex

        class NoVR:
            def run_stub(self, draft):
                raise AssertionError("preflight must bypass verifier")

        self.controller.agents["vr"] = NoVR()
        result = self.controller.run("request", gate1=lambda _: "approve")
        self.assertEqual(result["state"], "ESCALATED")
        self.assertEqual(len(result["rounds"]), 3)
        self.assertTrue(all(row["decision"] == "PREFLIGHT" for row in result["rounds"]))


if __name__ == "__main__":
    unittest.main()
