import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from agent.investigator import InvestigatorError, ReplyFormatError, investigate
from contracts.agent import (
    AgentState,
    DocId,
    FaultHypothesis,
    InvestigatorOutput,
    Passage,
    VerificationOutcome,
    VerifierOutput,
)
from contracts.dispute import DisputeRequest
from contracts.llm import LLMClient, LLMResponse, LLMUnavailableError
from contracts.result import FraudType, Route
from eval.corpus import corpus_available
from eval.schema import load_scenarios
from tests.conftest import VALID_PAYLOAD
from tests.test_investigator_tools import FakeRetriever

RBI, IDBI = DocId.RBI_2017_CIRCULAR, DocId.IDBI_POLICY


def _p(doc_id, clause_id):
    return Passage(doc_id=doc_id, clause_id=clause_id, text=f"text of {clause_id}", page=3)


RETRIEVER_RESULTS = {
    "third party breach reported within three working days":
        [_p(RBI, "6(ii)"), _p(IDBI, "Table-I.01")],
}
CLAUSES = [_p(IDBI, "5.20")]

SEARCH = json.dumps({
    "action": "search", "reason": "find the zero-liability rule",
    "searches": [{"query": "third party breach reported within three working days"}],
    "lookups": [{"doc_id": "IDBI_POLICY", "clause_id": "5.20"}],
})
ANSWER = {
    "action": "answer", "fraud_type": "card_not_present_fraud",
    "fault_hypothesis": "third_party", "draft_route": "provisional_credit",
    "draft_position": "Zero liability: third party breach reported in 1 working day "
                      "[RBI_2017_CIRCULAR:6(ii)].",
    "cited_clause_ids": ["RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:Table-I.01"],
}


def answer(**changes) -> str:
    return json.dumps({**ANSWER, **changes})


class ScriptedLLM:
    """Replies with the given texts in order; records every call."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, prompt, *, prompt_version, step, run_id, max_tokens=None):
        self.calls.append({"prompt": prompt, "prompt_version": prompt_version, "step": step,
                           "run_id": run_id, "max_tokens": max_tokens})
        return LLMResponse(text=self.replies.pop(0), prompt_tokens=100, completion_tokens=50,
                           cost_usd=0, model="scripted", latency_ms=1,
                           prompt_version=prompt_version, step=step)


def _state(**changes) -> AgentState:
    return AgentState(request=DisputeRequest.model_validate(VALID_PAYLOAD), run_id="run-1",
                      **changes)


def _run(*replies, state=None, retriever=None):
    llm = ScriptedLLM(*replies)
    retriever = retriever or FakeRetriever(RETRIEVER_RESULTS, CLAUSES)
    return investigate(state or _state(), retriever, llm), llm, retriever


def test_scripted_llm_fits_the_contract():
    assert isinstance(ScriptedLLM(), LLMClient)


def test_search_then_answer():
    state = _state()
    before = state.model_dump()
    out, llm, retriever = _run(SEARCH, answer(), state=state)

    assert isinstance(out, InvestigatorOutput)
    assert out.fraud_type == FraudType.card_not_present_fraud
    assert out.fault_hypothesis == FaultHypothesis.third_party
    assert out.draft_route == Route.provisional_credit
    assert out.working_days_delay == 1 and out.delay_basis.startswith("1 working day from")
    assert out.queries_run == ["third party breach reported within three working days"]
    assert [p.citation_id for p in out.passages] == [
        "RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:Table-I.01", "IDBI_POLICY:5.20"]
    assert out.cited_clause_ids == ["RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:Table-I.01"]

    assert len(llm.calls) == 2 and len(retriever.calls) == 2
    assert all(c["step"] == "investigator" and c["run_id"] == "run-1"
               and c["prompt_version"] == "v1" and c["max_tokens"] is None
               for c in llm.calls)
    assert "Nothing has been retrieved yet" in llm.calls[0]["prompt"]
    second = llm.calls[1]["prompt"]
    assert "[IDBI_POLICY:5.20] (page 3)" in second
    assert 'search "third party breach reported within three working days" ->' in second
    assert "get_clause IDBI_POLICY:5.20 -> found" in second
    assert state.model_dump() == before  # the graph owns the state


def test_citations_are_normalised_deduplicated_and_never_invented():
    out, _, _ = _run(SEARCH, answer(cited_clause_ids=[
        "RBI_2017_CIRCULAR : 6(ii)", "RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:Table-I.07",
        "IDBI_POLICY:5.20"]))
    assert out.cited_clause_ids == ["RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:5.20"]


def test_answer_before_searching_is_sent_back():
    out, llm, _ = _run(answer(), SEARCH, answer())
    assert len(llm.calls) == 3
    assert "answer rejected: nothing retrieved yet, search first" in llm.calls[1]["prompt"]
    assert "This is the last turn" in llm.calls[2]["prompt"]
    assert out.cited_clause_ids


def test_no_evidence_by_the_last_turn_is_an_error():
    with pytest.raises(InvestigatorError, match="without retrieving"):
        _run(answer(), answer(), answer())


def test_tools_on_the_last_turn_is_an_error():
    with pytest.raises(ReplyFormatError, match="last turn"):
        _run(SEARCH, SEARCH, SEARCH)


def test_malformed_reply_propagates():
    with pytest.raises(ReplyFormatError, match="no JSON object"):
        _run("The customer is not liable.")


def test_llm_and_retriever_errors_propagate():
    class Down(ScriptedLLM):
        def complete(self, *args, **kwargs):
            raise LLMUnavailableError("model unavailable: breaker open")

    with pytest.raises(LLMUnavailableError):
        investigate(_state(), FakeRetriever({}, []), Down())

    class ToolBudgetExceeded(Exception):
        pass

    class Exhausted(FakeRetriever):
        def search_corpus(self, query, k=5, doc_ids=None):
            raise ToolBudgetExceeded("20 tool calls used")

    with pytest.raises(ToolBudgetExceeded):
        _run(SEARCH, answer(), retriever=Exhausted({}, []))


def test_spent_wall_clock_stops_before_calling_the_model():
    state = _state(started_at=datetime.now(UTC) - timedelta(seconds=80),
                   max_wall_clock_seconds=75)
    llm = ScriptedLLM(SEARCH)
    with pytest.raises(InvestigatorError, match="wall-clock budget of 75s"):
        investigate(state, FakeRetriever({}, []), llm)
    assert llm.calls == []


def test_retry_shows_the_verifier_feedback():
    previous = InvestigatorOutput(
        fraud_type=FraudType.unknown, fault_hypothesis=FaultHypothesis.unclear,
        delay_basis="x", passages=[], draft_route=Route.provisional_credit,
        draft_position="Old position.")
    verifier = VerifierOutput(
        outcome=VerificationOutcome.unsupported, action="retry",
        retry_strategy="search IDBI Table-I for the savings account row",
        unsupported_claims=["liability is capped at Rs 5,000"],
        modifier_passages=[_p(IDBI, "Table-I-note")])
    _, llm, _ = _run(SEARCH, answer(),
                     state=_state(investigator=previous, verifier=verifier, attempts=1))
    prompt = llm.calls[0]["prompt"]
    assert "- Retry strategy: search IDBI Table-I for the savings account row" in prompt
    assert "- Unsupported claim: liability is capped at Rs 5,000" in prompt
    assert "- Provision to consider: [IDBI_POLICY:Table-I-note]" in prompt
    assert "- Previous draft position: Old position." in prompt


def test_first_draft_has_no_feedback():
    _, llm, _ = _run(SEARCH, answer())
    assert "VERIFIER FEEDBACK\nnone (first draft)" in llm.calls[0]["prompt"]


@pytest.mark.live
@pytest.mark.model
@pytest.mark.skipif(not corpus_available(), reason="corpus PDFs not present")
def test_live_investigation_of_s01():
    """Real gateway + real index: pytest -m "live and model" -s tests/test_investigator.py
    (needs `python -m ingest build` and llm-gateway on LLM_GATEWAY_URL or localhost:8100)."""
    from agent.llm_client import HttpLLMClient
    from retrieval import HybridRetriever

    scenario = next(s for s in load_scenarios() if s.id == "S01")
    client = HttpLLMClient(os.environ.get("LLM_GATEWAY_URL") or "http://localhost:8100")
    out = investigate(AgentState(request=scenario.request, run_id="live-s01"),
                      HybridRetriever.load(), client)
    print(out.model_dump_json(indent=2))
    assert out.working_days_delay == scenario.expected.working_days_delay
    assert out.queries_run and out.cited_clause_ids
    assert set(out.cited_clause_ids) <= {p.citation_id for p in out.passages}
