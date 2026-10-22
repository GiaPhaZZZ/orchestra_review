from pathlib import Path
from harness.agents.common import prompt_version


class Executor:
    key = "ex"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    def run_stub(self, improved_prompt: dict, *, issues: list[dict] | None = None,
                 previous_draft: str | None = None) -> str:
        # The deterministic scaffold keeps the prior draft when revising; real
        # execution consumes only the approved prompt and the issue list.
        if issues and previous_draft is not None:
            return previous_draft
        return improved_prompt["costar"]["objective"]
