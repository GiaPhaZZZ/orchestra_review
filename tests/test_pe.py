import json
import unittest
from pathlib import Path

from harness.agents.pe import PromptEngineer
from harness.agents.common import prompt_version
from harness.schemas import ImprovedPrompt


ROOT = Path(__file__).resolve().parents[1]
PE = PromptEngineer(ROOT / "harness" / "prompts" / "pe.md")


class StubClient:
    """Return prepared structured responses while recording PE's LLM call."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, agent, messages):
        self.calls.append((agent, messages))
        return json.dumps(self.payload), 23, 0.01


def result_for(request, **overrides):
    payload = {
        "costar": {
            "context": "Create the requested deliverable.",
            "objective": request,
            "style": "Clear and organized",
            "tone": "Direct",
            "audience": "General reader",
            "response": "Provide the requested result.",
        },
        "length_band": {"min": 150, "max": 300, "unit": "words"},
        "success_criteria": [
            "Include the requested deliverable",
            "Exclude information outside the request",
            "Return a clearly formatted response",
        ],
        "assumptions": [],
        "clarifying_questions": [],
        "changes_summary": "Organized the request into actionable instructions.",
    }
    payload.update(overrides)
    return payload


class PromptEngineerTests(unittest.TestCase):
    def check_request(self, request, payload):
        client = StubClient(payload)
        result = PE.run(request, client)
        self.assertIsInstance(result, ImprovedPrompt)
        self.assertEqual(len(client.calls), 1)
        agent, messages = client.calls[0]
        self.assertEqual(agent, "pe")
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(messages[0]["content"].startswith("version: 2"))
        self.assertEqual(messages[1], {"role": "user", "content": request})
        self.assertEqual(result.costar.objective, request)
        return result

    def test_unambiguous_request(self):
        request = "Summarize the meeting notes in three bullets."
        result = self.check_request(request, result_for(request))
        self.assertEqual(len(result.success_criteria), 3)
        self.assertFalse(result.clarifying_questions)

    def test_ambiguous_request_asks_at_most_two_questions(self):
        request = "Find a good option for me."
        payload = result_for(
            request,
            assumptions=["The user wants a recommendation."],
            clarifying_questions=["What type of option do you need?", "What budget or constraints should apply?"],
        )
        result = self.check_request(request, payload)
        self.assertEqual(len(result.clarifying_questions), 2)
        self.assertTrue(result.assumptions)

    def test_explicit_length_and_style_are_preserved(self):
        request = "Write exactly 100 words in a playful, rhyming style."
        payload = result_for(
            request,
            costar={"context": "", "objective": request, "style": "Rhyming", "tone": "Playful", "audience": "General reader", "response": "Exactly 100 words."},
            length_band={"min": 95, "max": 105, "unit": "words"},
        )
        result = self.check_request(request, payload)
        self.assertEqual(result.costar.style, "Rhyming")
        self.assertEqual(result.length_band.unit, "words")
        self.assertLess(result.length_band.min, 100)
        self.assertGreater(result.length_band.max, 100)

    def test_named_audience_is_retained(self):
        request = "Explain photosynthesis to a 10-year-old."
        payload = result_for(request, costar={"context": "", "objective": request, "style": "Simple", "tone": "Friendly", "audience": "10-year-old", "response": "Explain photosynthesis simply."})
        result = self.check_request(request, payload)
        self.assertEqual(result.costar.audience, "10-year-old")

    def test_conflicting_constraints_are_not_silently_resolved(self):
        request = "Explain every detail in exactly one sentence, and use at least ten sentences."
        payload = result_for(
            request,
            assumptions=["The one-sentence and ten-sentence requirements conflict and need resolution."],
            clarifying_questions=["Should the response contain exactly one sentence or at least ten?"]
        )
        result = self.check_request(request, payload)
        self.assertEqual(len(result.clarifying_questions), 1)
        self.assertIn("conflict", result.assumptions[0])

    def test_prompt_is_versioned_and_stub_satisfies_contract(self):
        self.assertEqual(prompt_version(PE.prompt_path), 2)
        result = PE.run_stub("Say hello")
        self.assertIsInstance(result, ImprovedPrompt)
        self.assertEqual(len(result.success_criteria), 3)


if __name__ == "__main__":
    unittest.main()
