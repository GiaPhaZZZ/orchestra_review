from pathlib import Path
from harness.agents.common import prompt_version
from harness.schemas import MemoryWrite


class MemoryCurator:
    key = "mc"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    def run_stub(self, request: str) -> list[MemoryWrite]:
        return []
