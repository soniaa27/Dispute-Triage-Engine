import pytest

from agent.investigator.facts import case_facts
from agent.investigator.tools import (
    MAX_SEARCHES_PER_TURN,
    Evidence,
    LookupRequest,
    SearchRequest,
    run_tools,
)
from contracts.agent import DocId, Passage
from contracts.dispute import DisputeRequest
from contracts.tools import Retriever
from eval.schema import load_scenarios
from tests.conftest import VALID_PAYLOAD

SCENARIOS = load_scenarios()
RBI, IDBI = DocId.RBI_2017_CIRCULAR, DocId.IDBI_POLICY


def _request(**changes) -> DisputeRequest:
    return DisputeRequest.model_validate({**VALID_PAYLOAD, **changes})


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_delay_matches_every_scenario(scenario):
    assert case_facts(scenario.request).working_days_delay == \
        scenario.expected.working_days_delay


def test_boundary_scenarios_are_flagged():
    flagged = {s.id for s in SCENARIOS if case_facts(s.request).on_boundary}
    assert {"S17", "S18"} <= flagged
    s17 = next(s for s in SCENARIOS if s.id == "S17")
    assert "boundary" in case_facts(s17.request).delay_basis


def test_delay_basis_states_dates_and_rule():
    facts = case_facts(_request(bank_alert_datetime="2026-09-01T14:06:00+05:30",
                                reported_datetime="2026-09-02T10:00:00+05:30"))
    assert facts.working_days_delay == 1 and facts.calendar_days == 1
    assert not facts.on_boundary
    assert facts.delay_basis.startswith(
        "1 working day from the bank alert on 2026-09-01 to the report on 2026-09-02")
    assert "Table 2" in facts.delay_basis and "Karnataka" in facts.delay_basis


def test_no_alert_means_no_delay():
    facts = case_facts(_request(bank_alert_datetime=None,
                                reported_datetime="2026-09-11T10:00:00+05:30"))
    assert facts.working_days_delay is None and facts.calendar_days == 10
    assert "No bank alert" in facts.delay_basis


def _p(doc_id: DocId, clause_id: str) -> Passage:
    return Passage(doc_id=doc_id, clause_id=clause_id, text=f"text of {clause_id}")


class FakeRetriever:
    """Fixed results per query; records calls in order."""

    def __init__(self, results: dict[str, list[Passage]], clauses: list[Passage]):
        self.results = results
        self.clauses = {p.citation_id: p for p in clauses}
        self.calls: list[tuple] = []

    def search_corpus(self, query, k=5, doc_ids=None):
        self.calls.append(("search", query, k, doc_ids))
        return self.results.get(query, [])

    def get_clause(self, doc_id, clause_id):
        self.calls.append(("get_clause", doc_id, clause_id))
        return self.clauses.get(f"{doc_id}:{clause_id}")


def test_fake_retriever_fits_the_contract():
    assert isinstance(FakeRetriever({}, []), Retriever)


def test_run_tools_collects_passages_and_observations():
    retriever = FakeRetriever(
        {"negligence OTP": [_p(IDBI, "5.12"), _p(IDBI, "Table-II.03")],
         "zero liability": [_p(RBI, "6(ii)"), _p(IDBI, "5.12")]},
        [_p(IDBI, "5.13")],
    )
    evidence = Evidence()
    observations = run_tools(
        retriever, evidence,
        [SearchRequest(query="negligence OTP", doc_ids=[IDBI]),
         SearchRequest(query="zero liability"), SearchRequest(query="no results here")],
        [LookupRequest(doc_id=IDBI, clause_id="5.13"),
         LookupRequest(doc_id=RBI, clause_id="99")],
    )
    assert [p.citation_id for p in evidence.passages] == [
        "IDBI_POLICY:5.12", "IDBI_POLICY:Table-II.03", "RBI_2017_CIRCULAR:6(ii)",
        "IDBI_POLICY:5.13"]
    assert evidence.queries_run == ["negligence OTP", "zero liability", "no results here"]
    assert retriever.calls[0] == ("search", "negligence OTP", 5, [IDBI])
    assert observations == [
        'search "negligence OTP" -> IDBI_POLICY:5.12, IDBI_POLICY:Table-II.03',
        'search "zero liability" -> RBI_2017_CIRCULAR:6(ii), IDBI_POLICY:5.12',
        'search "no results here" -> nothing',
        "get_clause IDBI_POLICY:5.13 -> found",
        "get_clause RBI_2017_CIRCULAR:99 -> not found",
    ]
    assert "IDBI_POLICY:5.13" in evidence and "RBI_2017_CIRCULAR:99" not in evidence


def test_per_turn_cap_limits_calls():
    retriever = FakeRetriever({}, [])
    searches = [SearchRequest(query=f"query {n}") for n in range(MAX_SEARCHES_PER_TURN + 2)]
    observations = run_tools(retriever, Evidence(), searches, [])
    assert len(retriever.calls) == MAX_SEARCHES_PER_TURN
    assert observations[-1] == "2 request(s) over the per-turn limit were not run"


def test_retriever_errors_propagate():
    class Budget(Exception):
        pass

    class Exhausted(FakeRetriever):
        def search_corpus(self, query, k=5, doc_ids=None):
            raise Budget("tool budget exceeded")

    with pytest.raises(Budget):
        run_tools(Exhausted({}, []), Evidence(), [SearchRequest(query="anything")], [])


def test_requests_are_validated():
    with pytest.raises(ValueError):
        SearchRequest(query="valid query", doc_ids=["NOT_A_DOC"])
    with pytest.raises(ValueError):
        SearchRequest(query="ab")
    with pytest.raises(ValueError):
        LookupRequest(doc_id=IDBI, clause_id="5.12", extra="x")
