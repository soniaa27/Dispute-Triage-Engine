"""Contracts between the two agents and the LangGraph state.

Investigator (Person 1) -> InvestigatorOutput -> Verifier (Person 2) -> VerifierOutput.
See docs/interfaces.md for the flow and the clause_id conventions.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from contracts.dispute import DisputeRequest
from contracts.result import FraudType, Route

CITATION_SEPARATOR = ":"


class DocId(StrEnum):
    """Corpus documents. Values match Citation.doc_id and corpus/README.md."""

    RBI_2017_CIRCULAR = "RBI_2017_CIRCULAR"
    IDBI_POLICY = "IDBI_POLICY"
    RBI_IOS_2026 = "RBI_IOS_2026"


class Passage(BaseModel):
    """One retrieved unit of corpus text, addressable by (doc_id, clause_id)."""

    model_config = ConfigDict(extra="forbid")

    doc_id: DocId
    clause_id: str = Field(min_length=1, description="See 'Clause ID conventions' in docs.")
    text: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1, description="1-based PDF page, if known.")
    score: float | None = Field(default=None, description="Retriever score; None for lookups.")

    @property
    def citation_id(self) -> str:
        """The "DOC_ID:clause_id" form used in InvestigatorOutput.cited_clause_ids."""
        return f"{self.doc_id}{CITATION_SEPARATOR}{self.clause_id}"


class FaultHypothesis(StrEnum):
    bank = "bank"
    third_party = "third_party"
    customer_negligence = "customer_negligence"
    unclear = "unclear"


class InvestigatorOutput(BaseModel):
    """Hand-off from the Investigator to the Verifier: a draft, not an answer."""

    model_config = ConfigDict(extra="forbid")

    fraud_type: FraudType
    fault_hypothesis: FaultHypothesis
    working_days_delay: int | None = Field(
        default=None, ge=0, description="From contracts.tools.compute_working_days; None if "
        "the start date (e.g. bank_alert_datetime) is unknown.",
    )
    delay_basis: str = Field(description="How the delay was computed and from which dates.")
    passages: list[Passage] = Field(description="Everything retrieved that the draft relies on.")
    queries_run: list[str] = Field(default_factory=list)
    draft_route: Route
    draft_position: str = Field(min_length=1)
    cited_clause_ids: list[str] = Field(
        default_factory=list, description='Each formatted "DOC_ID:clause_id".'
    )

    @model_validator(mode="after")
    def _citations_resolve_to_passages(self) -> "InvestigatorOutput":
        available = {p.citation_id for p in self.passages}
        missing = [c for c in self.cited_clause_ids if c not in available]
        if missing:
            raise ValueError(f"cited_clause_ids not present in passages: {missing}")
        return self


class VerificationOutcome(StrEnum):
    verified = "verified"
    unsupported = "unsupported"
    conflicting_provisions = "conflicting_provisions"
    out_of_scope = "out_of_scope"
    contested_fault = "contested_fault"


class VerifierOutput(BaseModel):
    """The Verifier's judgement on an InvestigatorOutput and what the graph does next."""

    model_config = ConfigDict(extra="forbid")

    outcome: VerificationOutcome
    supported_claims: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    modifier_passages: list[Passage] = Field(
        default_factory=list,
        description="Provisions found that modify or conflict with the draft's provisions.",
    )
    action: Literal["return", "retry", "escalate"]
    retry_strategy: str | None = None
    escalation_reason: str | None = None

    @model_validator(mode="after")
    def _action_is_consistent(self) -> "VerifierOutput":
        # A3: an unverified answer must never be returned.
        if self.action == "return" and (
            self.outcome != VerificationOutcome.verified or self.unsupported_claims
        ):
            raise ValueError("action=return requires outcome=verified and no unsupported_claims")
        if self.action == "retry" and not self.retry_strategy:
            raise ValueError("action=retry requires a retry_strategy")
        if self.action == "escalate" and not self.escalation_reason:
            raise ValueError("action=escalate requires an escalation_reason")
        return self


class AgentLimits(BaseSettings):
    """Loop bounds (requirement A1), read from AGENT_MAX_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    max_steps: int = Field(default=12, ge=1)
    max_tool_calls: int = Field(default=20, ge=1)
    # Below RUN_TIMEOUT_SECONDS (90) so the agent escalates before the worker's hard kill.
    max_wall_clock_seconds: float = Field(default=75, gt=0)


_LIMIT_FIELDS = ("max_steps", "max_tool_calls", "max_wall_clock_seconds")


class AgentState(BaseModel):
    """LangGraph state shared by the Investigator and Verifier nodes.

    Limits not passed explicitly are filled from AgentLimits (environment) on creation.
    """

    request: DisputeRequest
    run_id: str
    step_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    investigator: InvestigatorOutput | None = None
    verifier: VerifierOutput | None = None
    attempts: int = Field(default=0, ge=0)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    max_steps: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_wall_clock_seconds: float = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def _fill_limits_from_settings(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(data.get(f) is None for f in _LIMIT_FIELDS):
            limits = AgentLimits()
            data = {**data}
            for name in _LIMIT_FIELDS:
                if data.get(name) is None:
                    data[name] = getattr(limits, name)
        return data
