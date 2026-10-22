"""Strict contracts for the harness agents."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoStar(Contract):
    context: str
    objective: str
    style: str
    tone: str
    audience: str
    response: str


class LengthBand(Contract):
    min: int
    max: int
    unit: Literal["words", "tokens"] = "words"

    @model_validator(mode="after")
    def check_range(self):
        if not 0 < self.min < self.max:
            raise ValueError("length band must satisfy 0 < min < max")
        return self


class ImprovedPrompt(Contract):
    costar: CoStar
    length_band: LengthBand
    success_criteria: list[str]
    assumptions: list[str]
    clarifying_questions: list[str] = Field(default_factory=list)
    changes_summary: str

    @field_validator("success_criteria")
    @classmethod
    def check_criteria(cls, criteria: list[str]) -> list[str]:
        if not 3 <= len(criteria) <= 6:
            raise ValueError("success_criteria must contain 3 to 6 checkable items")
        for item in criteria:
            normalized = item.strip().lower()
            if not normalized or not any(
                marker in normalized
                for marker in ("include", "exclude", "exactly", "at least", "at most", "between", "must", "contains", "format", "verify", "cite", "list", "provide", "return")
            ):
                raise ValueError(f"success criterion is not checkable: {item!r}")
        return criteria

    @field_validator("clarifying_questions")
    @classmethod
    def max_questions(cls, questions: list[str]) -> list[str]:
        if len(questions) > 2:
            raise ValueError("clarifying_questions may contain at most 2 items")
        return questions


class Issue(Contract):
    metric: str
    severity: Literal["blocker", "major", "minor"]
    quote: str
    fix: str


class ClaimCheck(Contract):
    claim: str
    status: Literal["supported", "contradicted", "unverified"]
    evidence: str


class Verdict(Contract):
    decision: Literal["PASS", "REVISE", "PROMPT_PROBLEM", "NEEDS_HUMAN"]
    scores: dict[str, float]
    total: float
    issues: list[Issue]
    claims: list[ClaimCheck]
    length: dict
    prompt_diagnosis: str = ""

    @field_validator("scores")
    @classmethod
    def scores_in_unit_interval(cls, scores: dict[str, float]) -> dict[str, float]:
        if any(not 0.0 <= score <= 1.0 for score in scores.values()):
            raise ValueError("scores must be between 0 and 1")
        return scores

    @field_validator("total")
    @classmethod
    def total_in_unit_interval(cls, total: float) -> float:
        if not 0.0 <= total <= 1.0:
            raise ValueError("total must be between 0 and 1")
        return total

    @model_validator(mode="after")
    def validate_decision_details(self):
        if self.decision != "PASS" and not self.issues:
            raise ValueError("issues must be non-empty unless decision is PASS")
        if self.decision == "PROMPT_PROBLEM" and not self.prompt_diagnosis.strip():
            raise ValueError("prompt_diagnosis is required for PROMPT_PROBLEM")
        if self.length.get("status") not in {"TOO_SHORT", "OK", "TOO_LONG"}:
            raise ValueError("length.status must be TOO_SHORT, OK, or TOO_LONG")
        return self


class MemoryWrite(Contract):
    text: str
    type: Literal["preference", "prompt_pattern", "failure", "domain_fact"]
    confidence: float

    @field_validator("confidence")
    @classmethod
    def confidence_in_unit_interval(cls, confidence: float) -> float:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return confidence
