"""Schema of one evaluation scenario (requirement L3). See docs/evaluation.md."""

import json
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts import DisputeRequest, FraudType, Route
from contracts.agent import CITATION_SEPARATOR, FaultHypothesis
from contracts.tools import compute_working_days

DEFAULT_SCENARIOS = Path(__file__).parent / "scenarios.jsonl"
MAX_PASSAGE_WORDS = 60


class Category(StrEnum):
    clear_cut = "clear_cut"
    negligence = "negligence"
    fraud_type_coverage = "fraud_type_coverage"
    route_coverage = "route_coverage"
    conflict_escalation = "conflict_escalation"
    boundary = "boundary"
    out_of_scope = "out_of_scope"
    adversarial = "adversarial"


class CustomerLiability(StrEnum):
    zero = "zero"
    limited = "limited"
    full = "full"
    per_bank_policy = "per_bank_policy"
    not_applicable = "not_applicable"
    # Escalated cases where the corpus does not settle liability (conflicts, boundaries).
    undetermined = "undetermined"


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fraud_type: FraudType
    fault_hypothesis: FaultHypothesis
    working_days_delay: int | None = Field(ge=0)
    route: Route
    must_escalate: bool
    customer_liability: CustomerLiability
    liability_cap_inr: int | None = Field(default=None, ge=0)
    supporting_clauses: list[str] = Field(
        min_length=1,
        description='"DOC_ID:clause_id". The first is primary: supporting_passage comes from '
        "its document.",
    )
    supporting_passage: str = Field(min_length=10)
    must_not_cite: list[str] = Field(default_factory=list)
    basis_must_mention: list[str] = Field(
        default_factory=list,
        description="Terms the agent's liability_position + basis must contain (case-, comma- "
        "and whitespace-insensitive). Each must appear in supporting_passage.",
    )

    @field_validator("supporting_clauses", "must_not_cite")
    @classmethod
    def _citation_form(cls, ids: list[str]) -> list[str]:
        for cid in ids:
            if CITATION_SEPARATOR not in cid:
                raise ValueError(f"{cid!r} is not in DOC_ID:clause_id form")
        return ids

    @field_validator("supporting_passage")
    @classmethod
    def _short_excerpt(cls, text: str) -> str:
        if len(text.split()) > MAX_PASSAGE_WORDS:
            raise ValueError(f"supporting_passage must be at most {MAX_PASSAGE_WORDS} words")
        return text

    @model_validator(mode="after")
    def _escalation_consistent(self) -> "Expected":
        if self.must_escalate != (self.route == Route.escalate):
            raise ValueError("must_escalate must be true exactly when route is escalate")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^S\d{2}$")
    title: str = Field(min_length=3, max_length=120)
    category: Category
    request: DisputeRequest
    expected: Expected
    rationale: str = Field(min_length=10)

    @field_validator("rationale")
    @classmethod
    def _one_to_three_sentences(cls, text: str) -> str:
        # Split only before a capital letter, so "Rs. 5,000" or "7(ii). " do not count.
        sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip()) if s]
        if not 1 <= len(sentences) <= 3:
            raise ValueError("rationale must be 1 to 3 sentences")
        return text


def reporting_delay(request: DisputeRequest) -> int | None:
    """Working days from the bank alert's date to the reporting date, as written (+05:30)."""
    if request.bank_alert_datetime is None:
        return None
    return compute_working_days(
        request.bank_alert_datetime.date(),
        request.reported_datetime.date(),
        request.home_branch_state,
    )


def load_raw(path: Path = DEFAULT_SCENARIOS) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_scenarios(path: Path = DEFAULT_SCENARIOS) -> list[Scenario]:
    return [Scenario.model_validate(raw) for raw in load_raw(path)]
