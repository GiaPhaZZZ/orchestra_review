import unittest
from pathlib import Path

from harness.agents.ex import Executor
from harness.metrics import concision_score, count_length, fuzzy_issue_match, length_status, preflight
from harness.schemas import ImprovedPrompt


PROMPT = {
    "costar": {"context": "Context", "objective": "Do task", "style": "plain", "tone": "neutral",
               "audience": "reader", "response": "Markdown, required sections: Summary, Details"},
    "length_band": {"min": 2, "max": 4, "unit": "words"},
    "success_criteria": ["Include answer", "Exclude invention", "Return clear prose"],
    "assumptions": [], "changes_summary": "Approved", "clarifying_questions": [],
}


class MetricsTests(unittest.TestCase):
    def test_length_boundaries_and_units(self):
        band = {"min": 2, "max": 4, "unit": "words"}
        self.assertEqual(length_status("one", band)["status"], "TOO_SHORT")
        self.assertEqual(length_status("one two", band)["status"], "OK")
        self.assertEqual(length_status("one two three four", band)["status"], "OK")
        self.assertEqual(length_status("one two three four five", band)["status"], "TOO_LONG")
        self.assertEqual(count_length("Hi, there!", "words"), 2)
        self.assertEqual(count_length("Hi, there!", "tokens"), 4)

    def test_concision_is_one_in_band_and_falls_off_linearly(self):
        band = {"min": 2, "max": 4, "unit": "words"}
        self.assertEqual(concision_score("one two three", band), 1.0)
        self.assertEqual(concision_score("one", band), 0.5)
        self.assertEqual(concision_score("one two three four five", band), 0.75)
        self.assertEqual(concision_score("one two three four five six seven eight", band), 0.0)

    def test_fuzzy_issue_match_uses_metric_and_quote(self):
        first = [{"metric": "accuracy", "quote": "The result is exactly ten."}]
        self.assertTrue(fuzzy_issue_match(first, [{"metric": "accuracy", "quote": "The result is exact ten."}], 0.7))
        self.assertFalse(fuzzy_issue_match(first, [{"metric": "style", "quote": "The result is exactly ten."}], 0.7))
        self.assertFalse(fuzzy_issue_match(first, [{"metric": "accuracy", "quote": "A different quoted span."}], 0.8))

    def test_preflight_length_format_and_required_sections(self):
        too_short = dict(PROMPT, length_band={"min": 2, "max": 4, "unit": "words"})
        self.assertEqual(preflight(too_short, "Only" )["metric"], "length")
        self.assertEqual(preflight(PROMPT, "one two three four")["metric"], "format")
        complete = dict(PROMPT, costar={**PROMPT["costar"], "response": "Return JSON"},
                        length_band={"min": 1, "max": 4, "unit": "words"})
        self.assertEqual(preflight(complete, "{bad")["metric"], "format")
        self.assertEqual(preflight(PROMPT, "Summary\nOne.\n\nDetails\nTwo.")["metric"], "format")
        self.assertIsNone(preflight(PROMPT, "## Summary\nOne.\n\n## Details\nTwo."))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = Executor(Path(__file__).parents[1] / "harness" / "prompts" / "ex.md")

    def test_approved_prompt_is_sent_verbatim_and_not_as_a_rewrite(self):
        class Client:
            def __init__(self): self.messages = None
            def complete(self, agent, messages, *, json_mode=True):
                self.messages = messages
                self.assertion = (agent, json_mode)
                return "draft", 3, 0.1
        client = Client()
        draft = self.executor.run(client, PROMPT)
        self.assertEqual(draft, "draft")
        self.assertEqual(client.assertion, ("ex", False))
        self.assertEqual(client.messages[0]["content"], self.executor.approved_prompt(PROMPT))
        self.assertIn("OBJECTIVE: Do task", client.messages[0]["content"])

    def test_revision_receives_only_issue_list_and_previous_draft(self):
        class Client:
            def complete(self, agent, messages, *, json_mode=True):
                self.messages = messages
                return "revised", 5, 0.2
        client = Client()
        issue = {"metric": "clarity", "severity": "major", "quote": "unclear phrase", "fix": "Clarify it."}
        self.executor.run(client, PROMPT, issues=[issue], previous_draft="existing draft")
        content = client.messages[0]["content"]
        self.assertIn(self.executor.approved_prompt(PROMPT), content)
        self.assertIn("unclear phrase", content)
        self.assertIn("Clarify it.", content)
        self.assertIn("CURRENT DRAFT:\nexisting draft", content)
        self.assertNotIn("changes_summary", content)
        self.assertNotIn("success_criteria", content)

    def test_stub_uses_approved_objective_and_keeps_prior_draft_on_revision(self):
        self.assertEqual(self.executor.run_stub(PROMPT), "Do task")
        self.assertEqual(self.executor.run_stub(PROMPT, issues=[{"metric": "x"}], previous_draft="prior"), "prior")


if __name__ == "__main__":
    unittest.main()
