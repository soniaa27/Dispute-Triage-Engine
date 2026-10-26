import pytest
from pydantic import ValidationError

from contracts import DisputeRequest, FraudType, Route
from contracts.agent import (
    AgentState,
    DocId,
    FaultHypothesis,
    InvestigatorOutput,
    Passage,
    VerificationOutcome,
    VerifierOutput,
)
from contracts.llm import LLMClient, LLMResponse
from contracts.tools import Retriever

NEGLIGENCE = Passage(
    doc_id=DocId.RBI_2017_CIRCULAR,
    clause_id="7(i)",
    text="In cases where the loss is due to negligence by a customer, such as where he has "
    "shared the payment credentials, the customer will bear the entire loss until he "
    "reports the unauthorised transaction to the bank.",
    page=3,
    score=0.82,
)
DAYS = Passage(
    doc_id=DocId.IDBI_POLICY,
    clause_id="5.13",
    text="Number of Days: is calculated without considering the date of reporting and "
    "includes only working days in the Home Branch of the Client.",
    page=6,
)


def _investigator(**overrides) -> InvestigatorOutput:
    fields = {
        "fraud_type": FraudType.upi_social_engineering,
        "fault_hypothesis": FaultHypothesis.customer_negligence,
        "working_days_delay": 17,
        "delay_basis": "compute_working_days(2026-07-01, 2026-07-22, Maharashtra)",
        "passages": [NEGLIGENCE, DAYS],
        "queries_run": ["customer shared OTP liability"],
        "draft_route": Route.decline,
        "draft_position": "Customer bears the entire loss until reporting.",
        "cited_clause_ids": ["RBI_2017_CIRCULAR:7(i)", "IDBI_POLICY:5.13"],
    }
    return InvestigatorOutput(**{**fields, **overrides})


def test_passage_citation_id():
    assert NEGLIGENCE.citation_id == "RBI_2017_CIRCULAR:7(i)"


def test_investigator_rejects_citation_not_in_passages():
    with pytest.raises(ValidationError, match="not present in passages"):
        _investigator(cited_clause_ids=["RBI_2017_CIRCULAR:7(i)", "IDBI_POLICY:Table-II"])


def test_investigator_rejects_citation_with_wrong_doc():
    with pytest.raises(ValidationError):
        _investigator(cited_clause_ids=["IDBI_POLICY:7(i)"])


def test_investigator_round_trips_json():
    original = _investigator()
    assert InvestigatorOutput.model_validate_json(original.model_dump_json()) == original


def test_verifier_round_trips_json():
    original = VerifierOutput(
        outcome=VerificationOutcome.contested_fault,
        supported_claims=["Customer shared an OTP with a caller."],
        unsupported_claims=["Sharing an OTP with a bank impersonator is negligence."],
        modifier_passages=[DAYS],
        action="escalate",
        escalation_reason="Negligence vs third-party breach is not settled by the corpus.",
    )
    assert VerifierOutput.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"outcome": "unsupported", "action": "return"}, "action=return"),
        ({"outcome": "verified", "action": "return", "unsupported_claims": ["x"]},
         "action=return"),
        ({"outcome": "unsupported", "action": "retry"}, "retry_strategy"),
        ({"outcome": "out_of_scope", "action": "escalate"}, "escalation_reason"),
    ],
)
def test_verifier_action_must_be_consistent(fields, message):
    with pytest.raises(ValidationError, match=message):
        VerifierOutput(**fields)


def test_verifier_return_when_verified():
    out = VerifierOutput(outcome="verified", supported_claims=["a"], action="return")
    assert out.action == "return"


def test_llm_response_round_trips_json():
    original = LLMResponse(
        text="{}", prompt_tokens=120, completion_tokens=30, cost_usd=0.0, model="m",
        latency_ms=812.5, prompt_version="v1", step="investigator",
    )
    assert LLMResponse.model_validate_json(original.model_dump_json()) == original


def test_agent_state_limits_from_settings(monkeypatch, payload):
    monkeypatch.setenv("AGENT_MAX_STEPS", "5")
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "7")
    monkeypatch.setenv("AGENT_MAX_WALL_CLOCK_SECONDS", "30")
    state = AgentState(request=DisputeRequest.model_validate(payload), run_id="r-1")
    assert (state.max_steps, state.max_tool_calls, state.max_wall_clock_seconds) == (5, 7, 30)
    assert state.step_count == state.tool_call_count == state.attempts == 0
    assert state.started_at.tzinfo is not None


def test_agent_state_defaults_and_explicit_limits(monkeypatch, payload):
    for name in ("AGENT_MAX_STEPS", "AGENT_MAX_TOOL_CALLS", "AGENT_MAX_WALL_CLOCK_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    req = DisputeRequest.model_validate(payload)
    defaults = AgentState(request=req, run_id="r")
    assert (defaults.max_steps, defaults.max_tool_calls, defaults.max_wall_clock_seconds) == (
        12, 20, 75
    )
    assert AgentState(request=req, run_id="r", max_steps=3).max_steps == 3


def test_agent_state_round_trips_json(payload):
    state = AgentState(
        request=DisputeRequest.model_validate(payload), run_id="r-1", investigator=_investigator()
    )
    assert AgentState.model_validate_json(state.model_dump_json()) == state


def test_protocols_are_structural():
    class Lookup:
        def search_corpus(self, query, k=5, doc_ids=None):
            return []

        def get_clause(self, doc_id, clause_id):
            return None

    assert isinstance(Lookup(), Retriever)
    assert not isinstance(Lookup(), LLMClient)
