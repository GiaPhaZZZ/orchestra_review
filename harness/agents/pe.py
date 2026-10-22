from __future__ import annotations

from pathlib import Path

from harness.agents.common import prompt_version, strict_json
from harness.llm import LLMClient
from harness.schemas import ImprovedPrompt


class PromptEngineer:
    key = "pe"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    def run(self, request: str, client: LLMClient) -> ImprovedPrompt:
        """Rewrite a request into the strict structured PE contract."""
        if not request.strip():
            raise ValueError("request must not be empty")
        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        result, _, _ = strict_json(
            client,
            self.key,
            ImprovedPrompt,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request},
            ],
        )
        return result

    def run_stub(self, request: str) -> ImprovedPrompt:
        """Deterministic scaffold output for offline controller runs."""
        if not request.strip():
            raise ValueError("request must not be empty")
        return ImprovedPrompt.model_validate({
            "costar": {"context": "The user supplied this request: " + request, "objective": request, "style": "Clear and organized", "tone": "Helpful and direct", "audience": "The audience specified by the user, or a general reader if unspecified", "response": "Answer the objective while satisfying the stated constraints"},
            "length_band": {"min": 1, "max": 500, "unit": "words"},
            "success_criteria": ["Include a direct answer to the objective", "Exclude unsupported claims", "Return a readable, organized response"],
            "assumptions": ["Use a general reader as the audience when none is specified"], "clarifying_questions": [], "changes_summary": "Expanded the request into CO-STAR fields, a length band, and checkable response criteria",
        })
