from pathlib import Path
from harness.agents.common import prompt_version
from harness.llm import LLMClient
from harness.schemas import ImprovedPrompt


class Executor:
    key = "ex"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    @staticmethod
    def approved_prompt(improved_prompt: dict) -> str:
        """Render the human-approved CO-STAR fields without rewriting them."""
        prompt = ImprovedPrompt.model_validate(improved_prompt)
        return "\n".join(f"{name.upper()}: {value}" for name, value in prompt.costar.model_dump().items())

    def run(self, client: LLMClient, improved_prompt: dict, *, issues: list[dict] | None = None,
            previous_draft: str | None = None) -> str:
        content = self.approved_prompt(improved_prompt)
        if issues:
            content += "\n\nVERIFIER ISSUES (address only these):\n" + "\n".join(
                f"- metric={item['metric']}; quote={item['quote']!r}; fix={item['fix']}" for item in issues
            )
            if previous_draft is not None:
                content += "\n\nCURRENT DRAFT:\n" + previous_draft
        draft, _, _ = client.complete("ex", [{"role": "user", "content": content}], json_mode=False)
        return draft

    def run_stub(self, improved_prompt: dict, *, issues: list[dict] | None = None,
                 previous_draft: str | None = None) -> str:
        prompt = ImprovedPrompt.model_validate(improved_prompt)
        if issues and previous_draft is not None:
            return previous_draft
        return prompt.costar.objective
