from pathlib import Path
from harness.agents.common import prompt_version
from harness.schemas import MemoryWrite


class MemoryCurator:
    key = "mc"

    def __init__(self, prompt_path: Path):
        self.prompt_path = prompt_path
        self.version = prompt_version(prompt_path)

    def run_stub(self, request: str, *, gate2: str | None = None,
                 rejection_reason: str | None = None, accepted_preferences: list[str] | None = None,
                 passed_patterns: list[str] | None = None,
                 verified_facts: list[str] | None = None) -> list[MemoryWrite]:
        """Build only explicitly eligible memories; callers invoke this after Gate 2."""
        if gate2 == "reject" and rejection_reason and rejection_reason.strip():
            return [MemoryWrite(text=rejection_reason.strip(), type="failure", confidence=1.0)]
        if gate2 != "accept":
            return []
        candidates = (
            [(text, "preference") for text in (accepted_preferences or [])]
            + [(text, "prompt_pattern") for text in (passed_patterns or [])]
            + [(text, "domain_fact") for text in (verified_facts or [])]
        )
        writes = []
        for text, kind in candidates:
            text = text.strip()
            if text and len(writes) < 5:
                writes.append(MemoryWrite(text=text, type=kind, confidence=1.0))
        return writes
