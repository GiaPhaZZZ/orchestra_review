from pathlib import Path
from harness.schemas import ImprovedPrompt
from harness.agents.common import prompt_version


class PromptEngineer:
    key = "pe"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    def run_stub(self, request: str) -> ImprovedPrompt:
        return ImprovedPrompt.model_validate({
            "costar": {"context": "User request", "objective": request, "style": "Clear", "tone": "Helpful", "audience": "User", "response": "Answer the request"},
            "length_band": {"min": 1, "max": 500, "unit": "words"},
            "success_criteria": ["Include a direct answer", "Exclude unsupported claims", "Return a readable response"],
            "assumptions": [], "clarifying_questions": [], "changes_summary": "Stub prompt expansion",
        })
