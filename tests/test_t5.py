import json
import unittest
from pathlib import Path

from harness.agents.vr import Verifier, WEIGHTS
from harness.schemas import ImprovedPrompt, Verdict


ROOT = Path(__file__).resolve().parents[1]
PROMPT = {
    "costar": {"context": "facts", "objective": "Explain the result", "style": "plain",
               "tone": "neutral", "audience": "reader", "response": "Short answer"},
    "length_band": {"min": 3, "max": 6, "unit": "words"},
    "success_criteria": ["Include the result", "Verify factual claims", "Return a short answer"],
    "assumptions": [], "changes_summary": "approved", "clarifying_questions": [],
}


def proposed(draft, *, scores=None, claims=None, issues=None, decision="PASS", diagnosis=""):
    return Verdict(decision=decision, scores=scores or {name: 1.0 for name in WEIGHTS}, total=0.0,
                   issues=issues or [], claims=claims or [], length={"count": 4, "band": [3, 6], "status": "OK"},
                   prompt_diagnosis=diagnosis)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.verifier = Verifier(ROOT / "harness" / "prompts" / "vr.md")

    def test_input_builder_whitelists_only_approved_fields(self):
        input_data = self.verifier.build_input(PROMPT, ["source A"], "A draft.", "round 1: revise\n" + "OK")
        self.assertEqual(set(input_data), {
            "approved_prompt", "success_criteria", "sources", "current_draft", "earlier_verdict_summary"
        })
        encoded = json.dumps(input_data)
        for forbidden in ("reasoning", "notes", "private", "chain_of_thought", "EX"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(input_data["earlier_verdict_summary"], "round 1: revise OK")

    def test_weights_sum_and_total_is_computed(self):
        scores = {name: 0.5 for name in WEIGHTS}
        result = self.verifier.enforce(proposed("One two three four.", scores=scores), PROMPT,
                                       "One two three four.", has_sources=True)
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0)
        self.assertAlmostEqual(result.total, sum(
            (1.0 if name == "concision" else 0.5) * weight for name, weight in WEIGHTS.items()
        ))
        self.assertEqual(result.decision, "REVISE")

    def test_planted_criteria_defect_fails_weighted_gate(self):
        scores = {name: 1.0 for name in WEIGHTS}
        scores["instruction compliance"] = 0.2
        result = self.verifier.enforce(proposed("One two three four.", scores=scores), PROMPT,
                                       "One two three four.", has_sources=True)
        self.assertEqual(result.decision, "REVISE")
        self.assertLess(result.total, 0.85)

    def test_contradiction_is_blocker_and_unverified_claim_lowers_accuracy(self):
        draft = "The result is 42. Another value is unknown."
        contradiction = proposed(draft, claims=[{"claim": "The result is 42.", "status": "contradicted",
                                                  "evidence": "Source states 41."}])
        result = self.verifier.enforce(contradiction, PROMPT, draft, has_sources=True)
        self.assertEqual(result.decision, "REVISE")
        self.assertEqual(result.issues[0].severity, "blocker")
        self.assertEqual(result.issues[0].quote, "The result is 42.")
        unverified = proposed(draft, claims=[{"claim": "Another value is unknown.", "status": "unverified",
                                              "evidence": "No source confirms this."}])
        result = self.verifier.enforce(unverified, PROMPT, draft, has_sources=True)
        self.assertEqual(result.scores["factual accuracy"], 0.0)
        self.assertEqual(result.issues[0].severity, "major")
        self.assertEqual(result.issues[0].quote, "Another value is unknown.")

    def test_planted_length_defect_cannot_pass(self):
        draft = "One two three four five six seven eight nine."
        result = self.verifier.enforce(proposed(draft), PROMPT, draft, has_sources=True)
        self.assertEqual(result.decision, "REVISE")
        self.assertEqual(result.length["status"], "TOO_LONG")
        self.assertTrue(any(issue.metric == "length" for issue in result.issues))

    def test_no_sources_requires_non_factual_to_pass_even_with_clean_stub(self):
        factual = self.verifier.run_stub("One two three four.", PROMPT)
        allowed = self.verifier.run_stub("One two three four.", PROMPT, non_factual=True)
        self.assertNotEqual(factual.decision, "PASS")
        self.assertEqual(allowed.decision, "PASS")

    def test_optional_search_is_disabled_by_default_and_called_once_when_enabled(self):
        calls = []
        search = lambda query: calls.append(query) or "One two three four."
        default = Verifier(ROOT / "harness" / "prompts" / "vr.md", search_tool=search)
        self.assertFalse(default.search_enabled)
        self.assertEqual(calls, [])
        enabled = Verifier(ROOT / "harness" / "prompts" / "vr.md", search_enabled=True, search_tool=search)
        class Client:
            def complete(self, agent, messages, *, json_mode=True):
                if agent != "vr" or json_mode is not True:
                    raise AssertionError("verifier must use JSON-mode VR completion")
                self.payload = json.loads(messages[1]["content"])
                scores = {name: 1.0 for name in WEIGHTS}
                return (json.dumps({"decision": "PASS", "scores": scores, "total": 1.0, "issues": [],
                                    "claims": [{"claim": "One two three four.", "status": "supported",
                                                "evidence": "source"}],
                                    "length": {"count": 4, "band": [3, 6], "status": "OK"}}), 8, 0.1)
        client = Client()
        result = enabled.run(client, PROMPT, "One two three four.")
        self.assertEqual(calls, ["Explain the result"])
        self.assertEqual(client.payload["sources"], ["One two three four."])
        self.assertEqual(result.decision, "PASS")


if __name__ == "__main__":
    unittest.main()
