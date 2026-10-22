"""T1 memory seam; persistent Mem0 integration is outside the scaffold."""

class MemoryStore:
    def __init__(self):
        self.hits = 0
        self.misses = 0
        self.writes: list[dict] = []

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        self.misses += 1
        return []

    def write(self, item: dict, max_writes: int = 5) -> bool:
        if len(self.writes) >= max_writes:
            return False
        self.writes.append(item)
        return True
