"""Output contracts: what a run produces and what GET /runs/{run_id} returns."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    escalated = "escalated"
    failed = "failed"


TERMINAL_STATUSES = frozenset({RunStatus.completed, RunStatus.escalated, RunStatus.failed})


class Route(StrEnum):
    chargeback = "chargeback"
    provisional_credit = "provisional_credit"
    decline = "decline"
    escalate = "escalate"


class FraudType(StrEnum):
    card_not_present_fraud = "card_not_present_fraud"
    skimming = "skimming"
    upi_social_engineering = "upi_social_engineering"
    merchant_dispute = "merchant_dispute"
    duplicate_charge = "duplicate_charge"
    unknown = "unknown"


class Citation(BaseModel):
    doc_id: str = Field(min_length=1)
    clause_id: str = Field(min_length=1)
    text: str = Field(min_length=1, description="The retrieved passage the citation resolves to.")


class RunResult(BaseModel):
    """A liability recommendation. It never issues a credit or closes a dispute."""

    run_id: str
    status: RunStatus
    fraud_type: FraudType = FraudType.unknown
    route: Route | None = None
    liability_position: str | None = None
    basis: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    escalation_reason: str | None = None
    prompt_version: str
    trace: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "RunResult":
        if self.status == RunStatus.failed:
            if self.route is not None:
                raise ValueError("a failed run must not carry a route")
        elif self.status == RunStatus.escalated:
            if self.route != Route.escalate or not self.escalation_reason:
                raise ValueError("an escalated run needs route=escalate and an escalation_reason")
        elif self.status == RunStatus.completed:
            if self.route is None or self.route == Route.escalate:
                raise ValueError("a completed run needs a non-escalate route")
            if not self.citations:
                raise ValueError("a completed run must cite at least one provision")
        return self


class RunView(BaseModel):
    """Response body of GET /runs/{run_id}."""

    run_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    result: RunResult | None = None
    error: str | None = None
