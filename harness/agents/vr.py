from pathlib import Path
from harness.agents.common import prompt_version
from harness.schemas import Verdict


class Verifier:
    key = "vr"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    def run_stub(self, draft: str) -> Verdict:
        return Verdict.model_validate({
            "decision": "PASS", "scores": {"quality": 1.0, "factuality": 1.0}, "total": 1.0,
            "issues": [], "claims": [], "length": {"count": len(draft.split()), "band": [1, 500], "status": "OK"},
        })
