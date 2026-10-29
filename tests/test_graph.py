"""The LangGraph graph (agent/graph.py) and AGENT_MODE=graph: routing, retries, shared
limits, failures mid-run, and two scenarios end to end. The real Investigator and Verifier
run against scripted model replies; no model is called."""

import pytest

from agent import runner
from agent.graph import GraphSettings, run_graph
from agent.llm_client import LLMGatewayError
from contracts import RunResult, RunStatus
from contracts.llm import LLMUnavailableError
from eval.chunks import ChunkIndex
from ingest.build import load_chunks
from tests.agent_doubles import (
    IDBI,
    RBI,
    DictRetriever,
    ScriptedLLM,
    ScriptedSearch,
    answer,
    check,
    judge,
    lookups,
    scenario_request,
)
from tests.test_verifier import CLAUSES, LINKS, SUPPORTED, _request

SIX_II = "RBI_2017_CIRCULAR:6(ii)"
LOOKUP = lookups((RBI, "6(ii)"))
DRAFT = answer("card_not_present_fraud", "third_party", "provisional_credit", [SIX_II],
               "Zero liability: third party breach reported within three working days "
               "[RBI_2017_CIRCULAR:6(ii)].")
UNSUPPORTED = check([("Zero liability", True, SIX_II), ("Cap of Rs 5,000", False, "")],
                    ["customer negligence"])


def _graph(replies, retriever=None, retries=1, request=None, delay=0.0, **limits):
    llm = ScriptedLLM(replies, delay=delay)
    retriever = retriever or DictRetriever(
        CLAUSES, LINKS, {"customer negligence": ["IDBI_POLICY:Table-II.03"]})
    result = run_graph(request or _request(), "run-g", retriever, llm,
                       settings=GraphSettings(max_verify_retries=retries), **limits)
    RunResult.model_validate(result.model_dump())
    return result, llm, retriever


def _steps(result, step):
    return [t for t in result.trace if t["step"] == step]


def _nodes(result):
    return [t["node"] for t in _steps(result, "node")]


# ---------- routing ----------

def test_return_path_completes_with_the_verified_draft():
    result, llm, _ = _graph([LOOKUP, DRAFT, SUPPORTED, judge(False)])
    assert result.status == RunStatus.completed and result.route == "provisional_credit"
    assert result.prompt_version == "inv-v1+ver-v1"
    assert [(c.doc_id, c.clause_id) for c in result.citations] == [("RBI_2017_CIRCULAR",
                                                                    "6(ii)")]
    assert "Verified by a second agent" in result.basis
    assert llm.steps == ["investigator", "investigator", "verifier", "verifier"]
    assert _nodes(result) == ["investigate", "verify", "finalize"]
    for node in _steps(result, "node"):
        assert {"started_at", "ended_at", "latency_ms"} <= node.keys()
    (decision,) = _steps(result, "verifier_decision")
    assert (decision["outcome"], decision["action"]) == ("verified", "return")
    kinds = {t["step"] for t in result.trace}
    assert {"llm", "get_clause", "related", "search_corpus", "graph"} <= kinds
    assert [t["agent"] for t in _steps(result, "llm")] == llm.steps


def test_escalate_path_cites_both_sides_of_a_conflict():
    result, _, _ = _graph([LOOKUP, DRAFT, SUPPORTED,
                           judge(True, ["IDBI_POLICY:Table-II.03"], "Negligence applies.")])
    assert result.status == RunStatus.escalated and result.route == "escalate"
    assert "Negligence applies." in result.escalation_reason
    assert {c.clause_id for c in result.citations} == {"6(ii)", "Table-II.03"}
    assert result.liability_position == "undetermined"
    assert _steps(result, "verifier_decision")[0]["outcome"] == "conflicting_provisions"


def test_retry_sends_the_verifier_feedback_to_the_investigator():
    result, llm, _ = _graph([LOOKUP, DRAFT, UNSUPPORTED, LOOKUP, DRAFT, SUPPORTED,
                             judge(False)])
    assert result.status == RunStatus.completed
    assert _nodes(result) == ["investigate", "verify", "investigate", "verify", "finalize"]
    redraft_prompt = llm.calls[3][1]
    assert "Retry strategy:" in redraft_prompt and "Unsupported claim: Cap of Rs 5,000" in \
        redraft_prompt and "Previous draft position:" in redraft_prompt
    decisions = _steps(result, "verifier_decision")
    assert [d["action"] for d in decisions] == ["retry", "return"]
    assert _steps(result, "graph")[0]["attempts"] == 2


@pytest.mark.parametrize("retries", [0, 1])
def test_retry_limit_escalates_with_the_unsupported_claims(retries):
    replies = [LOOKUP, DRAFT, UNSUPPORTED] * (retries + 1)
    result, llm, _ = _graph(replies, retries=retries)
    assert result.status == RunStatus.escalated
    assert result.escalation_reason.startswith(f"Claims still unsupported after {retries} retr")
    assert "Cap of Rs 5,000" in result.escalation_reason
    assert _nodes(result).count("investigate") == retries + 1 and llm.replies == []


def test_step_limit_stops_the_retry_loop():
    result, _, _ = _graph([LOOKUP, DRAFT, UNSUPPORTED], retries=3, max_steps=2)
    assert result.status == RunStatus.escalated
    assert result.escalation_reason == "investigation limit reached (step limit of 2 reached)"


# ---------- shared limits ----------

def test_both_agents_share_one_tool_budget():
    # The Investigator's lookup and the Verifier's citation check use the 2 calls; the
    # Verifier's conflict search is the call over the limit.
    result, llm, retriever = _graph([LOOKUP, DRAFT, SUPPORTED], max_tool_calls=2)
    assert result.status == RunStatus.escalated
    assert result.escalation_reason == ("investigation limit reached (tool-call limit of 2 "
                                        "reached)")
    assert len(retriever.calls) == 2 and llm.steps[-1] == "verifier"
    verify_node = _steps(result, "node")[1]
    assert verify_node["node"] == "verify" and verify_node["outcome"] == "escalated"
    assert result.citations == []  # nothing from an unfinished verification


def test_wall_clock_is_enforced_between_nodes():
    result, llm, _ = _graph([LOOKUP, DRAFT], delay=0.15, max_wall_clock_seconds=0.25)
    assert result.status == RunStatus.escalated
    assert result.escalation_reason == ("investigation limit reached (wall-clock budget of "
                                        "0.25s spent before verify)")
    assert llm.steps == ["investigator", "investigator"]


# ---------- failures mid-run ----------

@pytest.mark.parametrize("error, reason", [
    (LLMUnavailableError("circuit open"), "model unavailable (circuit open)"),
    (LLMGatewayError("safety block", status_code=400, reason="safety"),
     "model unavailable (safety block)"),
])
def test_model_unavailable_mid_verification_escalates(error, reason):
    result, _, _ = _graph([LOOKUP, DRAFT, error])
    assert result.status == RunStatus.escalated and result.escalation_reason == reason
    assert _nodes(result) == ["investigate", "verify", "finalize"]
    assert _steps(result, "llm")[-1]["error"].startswith(type(error).__name__)


def test_malformed_verifier_reply_escalates_never_completes():
    result, _, _ = _graph([LOOKUP, DRAFT, "The draft looks right to me."])
    assert result.status == RunStatus.escalated
    assert "check reply was unusable" in result.escalation_reason
    assert _steps(result, "verifier_decision")[0]["outcome"] == "unsupported"


def test_investigator_error_escalates_with_its_message():
    result, _, _ = _graph(["not json"])
    assert result.status == RunStatus.escalated
    assert result.escalation_reason.startswith("no JSON object in reply")
    assert _nodes(result) == ["investigate", "finalize"]


def test_unexpected_exception_propagates_so_the_run_fails():
    class Broken(DictRetriever):
        def related(self, doc_id, clause_id):
            raise RuntimeError("index corrupted")

    with pytest.raises(RuntimeError, match="index corrupted"):
        _graph([LOOKUP, DRAFT], retriever=Broken(CLAUSES, LINKS))


def test_runner_graph_mode_uses_the_shared_clients(monkeypatch):
    retriever = DictRetriever(CLAUSES, LINKS)
    monkeypatch.setattr(runner, "get_retriever", lambda: retriever)
    monkeypatch.setattr(runner, "get_llm_client", lambda: ScriptedLLM([LOOKUP, DRAFT,
                                                                       SUPPORTED]))
    result = runner.run_agent(_request(), "run-m", mode="graph")
    assert result.status == RunStatus.completed and result.prompt_version == "inv-v1+ver-v1"


# ---------- scenarios on the real chunks ----------

def _assert_citations_resolve(result, data_dir):
    index = ChunkIndex(load_chunks(data_dir))
    for c in result.citations:
        assert index.check_citation(c.doc_id, c.clause_id, c.text) is None, c


def test_s01_skimming_is_verified_and_completed(real_corpus):
    data_dir, real = real_corpus
    retriever = ScriptedSearch(real, {"customer negligence card PIN": [(IDBI, "Table-II.03")]})
    cited = ["RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:Table-I.01", "IDBI_POLICY:5.20"]
    result, llm, _ = _graph([
        lookups((RBI, "6(ii)"), (IDBI, "Table-I.01"), (IDBI, "5.20")),
        answer("skimming", "third_party", "provisional_credit", cited,
               "Zero liability: a cloned card is a third party breach [IDBI_POLICY:5.20], "
               "reported 1 working day after the alert [RBI_2017_CIRCULAR:6(ii)], with "
               "shadow credit [IDBI_POLICY:Table-I.01]."),
        check([("A cloned card is a third party breach", True, "IDBI_POLICY:5.20"),
               ("Reported within three working days: zero liability", True,
                "RBI_2017_CIRCULAR:6(ii)"),
               ("Shadow credit for the full amount", True, "IDBI_POLICY:Table-I.01")],
              ["customer negligence card PIN"]),
        judge(False, reason="Table-II.03 needs negligence, which the facts do not show."),
    ], retriever=retriever, request=scenario_request("S01"))
    assert result.status == RunStatus.completed and result.route == "provisional_credit"
    assert result.fraud_type == "skimming"
    assert {f"{c.doc_id}:{c.clause_id}" for c in result.citations} == set(cited)
    _assert_citations_resolve(result, data_dir)
    assert len(llm.calls) == 4
    assert _steps(result, "graph")[0]["tool_calls"] <= 20  # default AGENT_MAX_TOOL_CALLS


@pytest.mark.parametrize("draft", ["negligence", "unclear"])
def test_s14_section_4_1_case_escalates_citing_both_provisions(real_corpus, draft):
    """OTP shared with a caller posing as the bank, reported 17 working days later. Either
    the Investigator drafts negligence and the Verifier finds the beyond-7-days provisions
    (judge), or it drafts "unclear" citing both and a hard rule escalates."""
    data_dir, real = real_corpus
    query = "third party breach reported beyond seven working days"
    retriever = ScriptedSearch(real, {query: [(RBI, "7(ii)-beyond-7"), (IDBI, "Table-I.10")]})
    negligence = ["RBI_2017_CIRCULAR:7(i)", "IDBI_POLICY:5.12"]
    beyond = ["RBI_2017_CIRCULAR:7(ii)-beyond-7", "IDBI_POLICY:Table-I.10"]
    if draft == "negligence":
        replies = [
            lookups((RBI, "7(i)"), (IDBI, "5.12")),
            answer("upi_social_engineering", "customer_negligence", "decline", negligence,
                   "Full liability: sharing an OTP is negligence [IDBI_POLICY:5.12] and the "
                   "customer bears the loss until reporting [RBI_2017_CIRCULAR:7(i)]."),
            check([("Sharing an OTP is negligence", True, "IDBI_POLICY:5.12"),
                   ("Customer bears the loss until reporting", True,
                    "RBI_2017_CIRCULAR:7(i)")], [query]),
            judge(True, beyond, "If the caller's deception is a third party breach, the "
                  "17-day delay falls under the beyond-7-working-days provisions instead."),
        ]
    else:
        replies = [
            lookups((RBI, "7(i)"), (IDBI, "5.12"), (RBI, "7(ii)-beyond-7"),
                    (IDBI, "Table-I.10")),
            answer("upi_social_engineering", "unclear", "escalate", negligence + beyond,
                   "Undetermined: negligence or a third party breach by deception."),
        ]
    result, llm, _ = _graph(replies, retriever=retriever, request=scenario_request("S14"))
    assert result.status == RunStatus.escalated and result.route == "escalate"
    assert result.liability_position == "undetermined"
    cited = {f"{c.doc_id}:{c.clause_id}" for c in result.citations}
    assert set(negligence + beyond) <= cited
    _assert_citations_resolve(result, data_dir)
    assert _nodes(result) == ["investigate", "verify", "finalize"]
    decision = _steps(result, "verifier_decision")[0]
    expected = "conflicting_provisions" if draft == "negligence" else "contested_fault"
    assert (decision["outcome"], decision["action"]) == (expected, "escalate")
    assert len(llm.calls) == (4 if draft == "negligence" else 2)
