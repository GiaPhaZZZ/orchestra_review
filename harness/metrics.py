"""Deterministic length, concision, issue matching, and draft preflight metrics."""

from __future__ import annotations
import json
import re
from difflib import SequenceMatcher
from typing import Any


def deliverable_text(text: str) -> str:
    """Exclude a conventional preamble when a draft marks its answer start."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s{0,3}#{1,6}\s+", line):
            return "\n".join(lines[index:]).strip()
        if line.strip().lower() in {"answer:", "draft:"}:
            return "\n".join(lines[index + 1:]).strip()
    return text.strip()


def count_length(text: str, unit: str = "words") -> int:
    text = deliverable_text(text)
    if unit == "words":
        return len(re.findall(r"\b[\w]+(?:['’][\w]+)?\b", text, flags=re.UNICODE))
    if unit == "tokens":
        return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))
    raise ValueError("unit must be 'words' or 'tokens'")


def length_status(text: str, band: dict[str, Any]) -> dict[str, Any]:
    unit = band.get("unit", "words")
    count = count_length(text, unit)
    minimum, maximum = int(band["min"]), int(band["max"])
    status = "TOO_SHORT" if count < minimum else "TOO_LONG" if count > maximum else "OK"
    return {"count": count, "band": [minimum, maximum], "unit": unit, "status": status}


def concision_score(text: str, band: dict[str, Any]) -> float:
    count = count_length(text, band.get("unit", "words"))
    minimum, maximum = int(band["min"]), int(band["max"])
    if minimum <= count <= maximum:
        return 1.0
    if count < minimum:
        return max(0.0, count / minimum)
    return max(0.0, 1.0 - (count - maximum) / maximum)


def fuzzy_issue_match(previous: list[dict], current: list[dict], threshold: float = 0.8) -> bool:
    for left in previous:
        for right in current:
            if left.get("metric") != right.get("metric"):
                continue
            a, b = left.get("quote", "").casefold().strip(), right.get("quote", "").casefold().strip()
            if a and b and SequenceMatcher(None, a, b).ratio() >= threshold:
                return True
    return False


def preflight(prompt: dict, draft: str) -> dict | None:
    band = prompt["length_band"]
    status = length_status(draft, band)
    if status["status"] != "OK":
        return {"metric": "length", "severity": "major", "quote": draft[:120],
                "fix": f"Adjust the deliverable to {band['min']}–{band['max']} {status['unit']} (currently {status['count']})."}
    response = prompt["costar"]["response"]
    clause = re.search(r"(?:required )?sections?\s*:\s*([^.;\n]+)", response, flags=re.IGNORECASE)
    required = [h.strip().strip("#-* ").strip("'\"`") for h in clause.group(1).split(",")] if clause else []
    missing = [heading for heading in required if heading and heading.casefold() not in draft.casefold()]
    if missing:
        return {"metric": "format", "severity": "major", "quote": draft[:120],
                "fix": "Add the required section(s): " + ", ".join(missing)}
    if "json" in response.casefold():
        try:
            json.loads(deliverable_text(draft))
        except (json.JSONDecodeError, TypeError):
            return {"metric": "format", "severity": "major", "quote": draft[:120],
                    "fix": "Return valid JSON as requested."}
    elif "markdown" in response.casefold():
        if not re.search(r"(?m)^\s{0,3}(?:#{1,6}\s+\S+|[-*+]\s+\S+|\d+[.)]\s+\S+)", draft):
            return {"metric": "format", "severity": "major", "quote": draft[:120],
                    "fix": "Use the requested Markdown headings or list formatting."}
    if re.search(r"\b\d+\s+(?:bullet points|bullets|items)\b", response, flags=re.IGNORECASE):
        expected = int(re.search(r"\b(\d+)\s+(?:bullet points|bullets|items)\b", response, flags=re.IGNORECASE).group(1))
        bullets = re.findall(r"(?m)^\s*[-*+]\s+\S+", draft)
        if len(bullets) != expected:
            return {"metric": "format", "severity": "major", "quote": draft[:120],
                    "fix": f"Return exactly {expected} bullet points."}
    return None


def score_table(rounds: list[dict]) -> str:
    lines = ["| Round | Decision | Total |", "|---:|---|---:|"]
    lines.extend(f"| {row['round']} | {row['decision']} | {row['total']:.2f} |" for row in rounds)
    return "\n".join(lines)
