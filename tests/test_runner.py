"""agent.runner in AGENT_MODE=investigator: mapping InvestigatorOutput onto RunResult, the
counting/tracing wrappers and error handling. investigate() is replaced by test doubles."""

import time

import pytest
from pydantic import ValidationError

from agent import runner
from agent.errors import ToolBudgetExceeded
from agent.investigator import InvestigatorError, ReplyFormatError
from agent.llm_client import LLMGatewayError
from contracts import DisputeRequest, FraudType, Route, RunStatus
from contracts.agent import DocId, FaultHypothesis, InvestigatorOutput, Passage
from contracts.llm import LLMResponse, LLMUnavailableError
from contracts.tools import Retriever
from eval.chunks import MAX_CITATION_WORDS, ChunkIndex
from eval.corpus import corpus_available
from ingest.build import build, load_chunks
from ingest.embed import HashEmbedder
from retrieval import HybridRetriever
from tests.conftest import VALID_PAYLOAD
from tests.test_investigator_tools import FakeRetriever
from worker.settings import WorkerSettings

RBI, IDBI = DocId.RBI_2017_CIRCULAR, DocId.IDBI_POLICY
QUERY = "third party breach reported within three working days"
SIX_II = Passage(doc_id=RBI, clause_id="6(ii)", text="Zero liability of a customer ...", page=3)
TABLE_ROW = Passage(doc_id=IDBI, clause_id="Table-I.01", text="Table - I, S. No 01", page=6)

corpus = pytest.mark.skipif(not corpus_available(), reason="corpus PDFs not present")


class StubLLM:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def complete(self, prompt, *, prompt_version, step, run_id, max_tokens=None):
        if self.error:
            raise self.error
        return LLMResponse(text="{}", prompt_tokens=120, completion_tokens=40, cost_usd=0,
                           model="stub", latency_ms=1, prompt_version=prompt_version, step=step)


def _request() -> DisputeRequest:
    return DisputeRequest.model_validate(VALID_PAYLOAD)


def _draft(passages, cited, route=Route.provisional_credit, position="Zero liability."):
    return InvestigatorOutput(
        fraud_type=FraudType.card_not_present_fraud,
        fault_hypothesis=FaultHypothesis.third_party, working_days_delay=1,
        delay_basis="1 working day from the bank alert on 2026-09-01 to the report.",
        passages=passages, queries_run=[QUERY], draft_route=route, draft_position=position,
        cited_clause_ids=cited)


def fake_investigate(route=Route.provisional_credit, cited=None, lookups=()):
    """A double for investigate(): one model call, one search, then lookups; returns a
    draft over everything retrieved, citing `cited` (default: all of it)."""

    def investigate(state, retriever, llm):
        llm.complete("prompt", prompt_version="v1", step="investigator", run_id=state.run_id)
        passages = list(retriever.search_corpus(QUERY, k=5))
        for doc_id, clause_id in lookups:
            passages.append(retriever.get_clause(doc_id, clause_id))
        ids = [p.citation_id for p in passages] if cited is None else cited
        return _draft(passages, ids, route=route)

    return investigate


def raising(exc: Exception, after=None):
    def investigate(state, retriever, llm):
        if after:
            after(state, retriever, llm)
        raise exc

    return investigate


@pytest.fixture
def wired(monkeypatch):
    """Investigator mode over a fixed retriever and a stub LLM; returns the retriever."""
    retriever = FakeRetriever({QUERY: [SIX_II, TABLE_ROW]}, [])
    llm = StubLLM()
    monkeypatch.setattr(runner, "get_retriever", lambda: retriever)
    monkeypatch.setattr(runner, "get_llm_client", lambda: llm)
    return retriever


def _run(monkeypatch, investigate) -> runner.RunResult:
    monkeypatch.setattr(runner, "investigate", investigate)
    return runner.run_agent(_request(), "run-1", mode="investigator")


# ---------- mode switch ----------

def test_agent_mode_defaults_to_fake_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("AGENT_MODE", raising=False)
    assert WorkerSettings(database_url="x", redis_url="y").agent_mode == "fake"
    monkeypatch.setenv("AGENT_MODE", "investigator")
    assert WorkerSettings(database_url="x", redis_url="y").agent_mode == "investigator"
    monkeypatch.setenv("AGENT_MODE", "graph")
    assert WorkerSettings(database_url="x", redis_url="y").agent_mode == "graph"
    monkeypatch.setenv("AGENT_MODE", "langchain")
    with pytest.raises(ValidationError):
        WorkerSettings(database_url="x", redis_url="y")
    with pytest.raises(ValueError, match="unknown AGENT_MODE"):
        runner.run_agent(_request(), "r", mode="langchain")


def test_fake_mode_is_still_the_placeholder(monkeypatch):
    monkeypatch.setattr(runner, "FAKE_DELAY_SECONDS", 0)
    result = runner.run_agent(_request(), "r")
    assert result.prompt_version == "fake-0" and result.status == RunStatus.completed


# ---------- a normal draft ----------

def test_normal_draft_completes_with_cited_basis_and_trace(monkeypatch, wired):
    result = _run(monkeypatch, fake_investigate())

    assert result.status == RunStatus.completed
    assert result.route == Route.provisional_credit
    assert result.fraud_type == FraudType.card_not_present_fraud
    assert result.liability_position == "Zero liability."
    assert result.prompt_version == "v1+unverified"
    assert [(c.doc_id, c.clause_id) for c in result.citations] == [
        ("RBI_2017_CIRCULAR", "6(ii)"), ("IDBI_POLICY", "Table-I.01")]
    assert "RBI_2017_CIRCULAR:6(ii)" in result.basis and "IDBI_POLICY:Table-I.01" in result.basis
    assert "Fault hypothesis: third_party" in result.basis and "1 working day" in result.basis
    assert result.escalation_reason is None

    steps = [t["step"] for t in result.trace]
    assert steps == ["llm", "search_corpus", "investigator"]
    llm, search, summary = result.trace
    assert llm["agent"] == "investigator" and llm["prompt_tokens"] == 120
    assert search["query"] == QUERY
    assert search["results"] == ["RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:Table-I.01"]
    assert all("latency_ms" in t for t in result.trace)
    assert summary == {**summary, "outcome": "completed", "llm_calls": 1, "tool_calls": 1}


def test_split_parts_are_cited_once_under_the_base_id(monkeypatch, wired):
    part1 = Passage(doc_id=RBI, clause_id="5#1", text="first part", page=2)
    part2 = Passage(doc_id=RBI, clause_id="5#2", text="second part", page=3)
    draft = _draft([part1, part2], ["RBI_2017_CIRCULAR:5#1", "RBI_2017_CIRCULAR:5#2"])
    citations = runner.citations_from_draft(draft)
    assert [(c.clause_id, c.text) for c in citations] == [("5", "first part")]


def test_citation_text_is_capped_at_the_word_limit():
    long = Passage(doc_id=RBI, clause_id="7(i)", text=" ".join(f"w{i}" for i in range(90)))
    (citation,) = runner.citations_from_draft(_draft([long], ["RBI_2017_CIRCULAR:7(i)"]))
    assert citation.text.split() == [f"w{i}" for i in range(MAX_CITATION_WORDS)]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    build(out_dir=out, embedder=HashEmbedder())
    return out


@corpus
def test_citations_from_real_chunks_pass_the_eval_check(monkeypatch, built):
    """Real chunk text through the real retriever: every citation resolves under eval's
    chunk-based check (base id, at most 60 words, verbatim span)."""
    real = HybridRetriever.load(built, embedder=HashEmbedder())
    assert isinstance(real, Retriever)
    monkeypatch.setattr(runner, "get_retriever", lambda: real)
    monkeypatch.setattr(runner, "get_llm_client", lambda: StubLLM())
    lookups = [(RBI, "5#2"), (RBI, "6(ii)"), (IDBI, "Table-I.03"), (IDBI, "5.13"),
               (RBI, "Table-2")]
    result = _run(monkeypatch, fake_investigate(lookups=lookups))

    retrieved = [real.get_clause(d, c) for d, c in lookups]
    assert any(len(p.text.split()) > MAX_CITATION_WORDS for p in retrieved)  # cap exercised
    index = ChunkIndex(load_chunks(built))
    assert result.status == RunStatus.completed and len(result.citations) >= len(lookups)
    for c in result.citations:
        assert "#" not in c.clause_id
        assert index.check_citation(c.doc_id, c.clause_id, c.text) is None, c
    assert ("RBI_2017_CIRCULAR", "5") in {(c.doc_id, c.clause_id) for c in result.citations}
    assert [t["step"] for t in result.trace].count("get_clause") == len(lookups)


# ---------- escalation ----------

def test_escalate_draft_is_an_escalated_run(monkeypatch, wired):
    result = _run(monkeypatch, fake_investigate(route=Route.escalate))
    assert result.status == RunStatus.escalated and result.route == Route.escalate
    assert result.escalation_reason.startswith("The Investigator could not settle the case")
    assert "Zero liability." in result.escalation_reason
    assert result.citations  # kept for the analyst
    assert result.trace[-1]["outcome"] == "escalated"


def test_draft_without_citations_is_never_completed(monkeypatch, wired):
    result = _run(monkeypatch, fake_investigate(cited=[]))
    assert result.status == RunStatus.escalated and result.route == Route.escalate
    assert "cites no retrieved provision" in result.escalation_reason
    assert "Clauses relied on: none" in result.basis


# ---------- errors ----------

@pytest.mark.parametrize("error", [
    LLMUnavailableError("model unavailable: provider_unavailable (breaker open)"),
    LLMGatewayError("llm-gateway returned HTTP 400", status_code=400, reason="bad_request"),
])
def test_model_unavailable_escalates(monkeypatch, wired, error):
    monkeypatch.setattr(runner, "get_llm_client", lambda: StubLLM(error=error))
    result = _run(monkeypatch, fake_investigate())
    assert result.status == RunStatus.escalated and result.route == Route.escalate
    assert result.escalation_reason.startswith("model unavailable (")
    assert result.citations == [] and result.liability_position is None
    assert result.prompt_version == "v1+unverified"
    assert result.trace[0]["step"] == "llm" and type(error).__name__ in result.trace[0]["error"]
    assert result.trace[-1]["outcome"] == "escalated: model unavailable"


def test_tool_budget_escalates_before_the_call_over_the_limit(monkeypatch, wired):
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "2")

    def search_forever(state, retriever, llm):
        while True:
            retriever.search_corpus(QUERY)

    result = _run(monkeypatch, search_forever)
    assert result.status == RunStatus.escalated
    assert result.escalation_reason == ("investigation limit reached "
                                        "(tool-call limit of 2 reached)")
    assert len(wired.calls) == 2  # the third call never reached the retriever
    assert [t["step"] for t in result.trace] == ["search_corpus", "search_corpus",
                                                  "investigator"]


def test_wall_clock_spent_escalates_as_a_limit(monkeypatch, wired):
    monkeypatch.setenv("AGENT_MAX_WALL_CLOCK_SECONDS", "0.05")
    result = _run(monkeypatch, raising(
        InvestigatorError("wall-clock budget of 0.05s spent before investigator turn 2"),
        after=lambda *_: time.sleep(0.1)))
    assert result.status == RunStatus.escalated
    assert result.escalation_reason.startswith("investigation limit reached (wall-clock")


def test_unusable_reply_escalates_with_its_message(monkeypatch, wired):
    result = _run(monkeypatch, raising(ReplyFormatError("model reply has no JSON object")))
    assert result.status == RunStatus.escalated and result.route == Route.escalate
    assert result.escalation_reason == "model reply has no JSON object"
    assert result.trace[-1]["outcome"] == "escalated: investigator error"


def test_unexpected_exception_propagates_so_the_run_fails(monkeypatch, wired):
    with pytest.raises(RuntimeError, match="index corrupted"):
        _run(monkeypatch, raising(RuntimeError("index corrupted")))


def test_worker_marks_unexpected_exception_failed(monkeypatch, wired, database_url, engine):
    from sqlalchemy.orm import Session

    from common.db import Run
    from worker import tasks

    settings = WorkerSettings(database_url=database_url, redis_url="redis://unused",
                              agent_mode="investigator")
    tasks.get_session_factory.cache_clear()
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(runner, "investigate", raising(RuntimeError("index corrupted")))
    with Session(engine) as s, s.begin():
        s.add(Run(id="run-x", status="queued", correlation_id="c",
                  request_json=_request().model_dump(mode="json")))
    try:
        assert tasks.process_run("run-x", "c") == "failed"
        with Session(engine) as s:
            run = s.get(Run, "run-x")
            assert run.result_json is None and "index corrupted" in run.error
    finally:
        tasks.get_session_factory.cache_clear()


# ---------- the wrappers ----------

def test_traced_retriever_counts_every_tool_and_records_errors():
    class Broken(FakeRetriever):
        def related(self, doc_id, clause_id):
            raise KeyError("boom")

    trace = []
    traced = runner.TracedRetriever(Broken({}, [TABLE_ROW]), trace, max_calls=3)
    assert isinstance(traced, Retriever)
    assert traced.get_clause(IDBI, "Table-I.01") == TABLE_ROW
    assert traced.get_clause(IDBI, "Nope") is None
    with pytest.raises(KeyError):
        traced.related(IDBI, "Table-I.01")
    with pytest.raises(ToolBudgetExceeded):
        traced.search_corpus(QUERY)
    assert traced.calls == 3
    assert [(t["step"], t.get("found"), "error" in t) for t in trace] == [
        ("get_clause", True, False), ("get_clause", False, False), ("related", None, True)]
