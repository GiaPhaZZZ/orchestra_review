from pathlib import Path
from harness.agents.common import prompt_version


class Executor:
    key = "ex"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    def run_stub(self, improved_prompt: dict) -> str:
        return improved_prompt["costar"]["objective"]
