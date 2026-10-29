"""Agent entry point called by the worker: run_agent(req, run_id, mode) -> RunResult.

AGENT_MODE (worker settings) picks what runs:
- fake: deterministic placeholder after a fixed delay, no model or retrieval ("fake-0").
- investigator: the Investigator alone; its draft is returned unverified (prompt_version
  "<version>+unverified"). Kept for comparison and debugging.
- graph: the LangGraph graph, Investigator then Verifier (agent/graph.py), prompt_version
  "inv-<version>+ver-<version>". Only a verified draft completes; everything else escalates.

Keep the signature and the RunResult contract unchanged so the worker and API need no edits.

In investigator and graph mode a run never returns a confident answer it did not finish:
the model being unavailable, the tool or time budget running out, or an unusable model reply
all escalate to an analyst; any other exception propagates and the worker marks the run
failed.
"""

import time
from functools import cache
from typing import Any, Literal

import structlog

from agent.errors import LIMIT_REACHED, MODEL_UNAVAILABLE, ToolBudgetExceeded
from agent.investigator import InvestigatorError, investigate
from agent.llm_client import LLMGatewayError
from agent.prompts import CURRENT_PROMPT_VERSION
from agent.tracking import TracedLLM, TracedRetriever, ms_since
from contracts import Channel, Citation, DisputeRequest, FraudType, Route, RunResult, RunStatus
from contracts.agent import AgentState, InvestigatorOutput
from contracts.llm import LLMClient, LLMUnavailableError
from contracts.tools import Retriever

AgentMode = Literal["fake", "investigator", "graph"]

log = structlog.get_logger("agent.runner")

# ---------- Placeholder (AGENT_MODE=fake) ----------

PROMPT_VERSION = "fake-0"
FAKE_DELAY_SECONDS = 2.0

_RBI_CITATION = Citation(
    doc_id="RBI_2017_CIRCULAR",
    clause_id="FAKE-PLACEHOLDER-1",
    text="[fake] Placeholder for a retrieved RBI circular passage on customer liability.",
)
_IDBI_CITATION = Citation(
    doc_id="IDBI_POLICY",
    clause_id="FAKE-PLACEHOLDER-2",
    text="[fake] Placeholder for a retrieved IDBI Bank policy passage.",
)


def _fake_fraud_type(req: DisputeRequest, mentions_otp: bool) -> FraudType:
    if req.channel == Channel.UPI:
        return FraudType.upi_social_engineering if mentions_otp else FraudType.unknown
    if req.channel == Channel.CARD_NOT_PRESENT:
        return FraudType.card_not_present_fraud
    if req.channel in (Channel.CARD_PRESENT, Channel.ATM):
        return FraudType.skimming
    return FraudType.unknown


def run_fake(req: DisputeRequest, run_id: str) -> RunResult:
    """FAKE: deterministic result after a fixed delay. Not grounded in any retrieval."""
    started = time.monotonic()
    time.sleep(FAKE_DELAY_SECONDS)
    mentions_otp = "otp" in req.complaint_text.lower()
    trace = [
        {
            "step": "fake_agent",
            "note": "placeholder; no model or retrieval calls made",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    ]
    fraud_type = _fake_fraud_type(req, mentions_otp)

    if mentions_otp:
        return RunResult(
            run_id=run_id,
            status=RunStatus.escalated,
            fraud_type=fraud_type,
            route=Route.escalate,
            liability_position="undetermined",
            basis="[fake] OTP disclosure: negligence vs third-party breach is contested.",
            citations=[_RBI_CITATION, _IDBI_CITATION],
            escalation_reason="Fault attribution contested: customer reports sharing an OTP.",
            prompt_version=PROMPT_VERSION,
            trace=trace,
        )
    return RunResult(
        run_id=run_id,
        status=RunStatus.completed,
        fraud_type=fraud_type,
        route=Route.provisional_credit,
        liability_position="[fake] zero customer liability",
        basis="[fake] Placeholder basis; no provisions were actually retrieved.",
        citations=[_RBI_CITATION],
        prompt_version=PROMPT_VERSION,
        trace=trace,
    )


def run_agent(req: DisputeRequest, run_id: str, mode: AgentMode = "fake") -> RunResult:
    if mode == "graph":
        return run_graph_mode(req, run_id)
    if mode == "investigator":
        return run_investigator(req, run_id)
    if mode == "fake":
        return run_fake(req, run_id)
    raise ValueError(f"unknown AGENT_MODE {mode!r}")


# ---------- Investigator (AGENT_MODE=investigator) ----------

UNVERIFIED_SUFFIX = "+unverified"
# Citation rules (docs/interfaces.md): a verbatim span of at most this many words of the
# clause's chunk text, checked by eval.chunks.ChunkIndex.check_citation.
MAX_CITATION_WORDS = 60
PART_SEPARATOR = "#"  # ingest.chunks: "5#2" is part 2 of clause "5"


@cache
def get_retriever() -> Retriever:
    """One HybridRetriever per worker process (index, chunks and embedding model in memory)."""
    from retrieval import HybridRetriever

    return HybridRetriever.load()


@cache
def get_llm_client() -> LLMClient:
    """One HttpLLMClient per worker process (LLM_GATEWAY_URL from the environment)."""
    from agent.llm_client import HttpLLMClient

    return HttpLLMClient()


def warm_up() -> None:
    """Load the retriever and LLM client now, e.g. in the worker before it forks jobs."""
    get_retriever()
    get_llm_client()


def _wall_clock_spent(state: AgentState) -> bool:
    elapsed = time.time() - state.started_at.timestamp()
    return elapsed >= state.max_wall_clock_seconds


def _span(text: str, max_words: int = MAX_CITATION_WORDS) -> str:
    """The first max_words words of a passage: verbatim under the eval's whitespace-blind
    comparison, and within the citation word limit."""
    return " ".join(text.split()[:max_words])


def citations_from_draft(draft: InvestigatorOutput) -> list[Citation]:
    """One citation per cited clause, under its base clause id ("5#2" -> "5"), with a
    verbatim span of the retrieved passage. Every cited id is in draft.passages (enforced by
    InvestigatorOutput); parts of one clause are cited once."""
    passages = {p.citation_id: p for p in draft.passages}
    citations, seen = [], set()
    for citation_id in draft.cited_clause_ids:
        passage = passages[citation_id]
        clause_id = passage.clause_id.split(PART_SEPARATOR, 1)[0]
        key = (passage.doc_id, clause_id)
        if key in seen:
            continue
        seen.add(key)
        citations.append(Citation(doc_id=str(passage.doc_id), clause_id=clause_id,
                                  text=_span(passage.text)))
    return citations


def result_from_draft(draft: InvestigatorOutput, run_id: str, prompt_version: str,
                      trace: list[dict[str, Any]]) -> RunResult:
    citations = citations_from_draft(draft)
    relied_on = ", ".join(f"{c.doc_id}:{c.clause_id}" for c in citations)
    basis = (f"Fault hypothesis: {draft.fault_hypothesis}. Reporting delay: "
             f"{draft.delay_basis} Clauses relied on: {relied_on or 'none'}.")
    fields = dict(run_id=run_id, fraud_type=draft.fraud_type,
                  liability_position=draft.draft_position, basis=basis, citations=citations,
                  prompt_version=prompt_version, trace=trace)
    if draft.draft_route == Route.escalate:
        return RunResult(status=RunStatus.escalated, route=Route.escalate,
                         escalation_reason="The Investigator could not settle the case and "
                         f"recommends analyst review: {draft.draft_position}", **fields)
    if not citations:
        # A completed run must cite a retrieved provision; never answer without one.
        return RunResult(status=RunStatus.escalated, route=Route.escalate,
                         escalation_reason="The draft cites no retrieved provision "
                         f"(drafted route: {draft.draft_route}).", **fields)
    return RunResult(status=RunStatus.completed, route=draft.draft_route, **fields)


def _escalated(run_id: str, reason: str, prompt_version: str,
               trace: list[dict[str, Any]]) -> RunResult:
    return RunResult(run_id=run_id, status=RunStatus.escalated, route=Route.escalate,
                     escalation_reason=reason, prompt_version=prompt_version, trace=trace)


def run_investigator(req: DisputeRequest, run_id: str) -> RunResult:
    state = AgentState(request=req, run_id=run_id)
    trace: list[dict[str, Any]] = []
    retriever = TracedRetriever(get_retriever(), trace, state.max_tool_calls)
    llm = TracedLLM(get_llm_client(), trace)
    prompt_version = CURRENT_PROMPT_VERSION + UNVERIFIED_SUFFIX
    started = time.perf_counter()

    def summary(outcome: str) -> None:
        # Added before the RunResult is built: RunResult copies the trace list.
        trace.append({"step": "investigator", "outcome": outcome, "llm_calls": llm.calls,
                      "tool_calls": retriever.calls, "latency_ms": ms_since(started)})

    reason = None
    try:
        draft = investigate(state, retriever, llm)
    except (LLMUnavailableError, LLMGatewayError) as exc:
        reason, error = MODEL_UNAVAILABLE, exc
    except ToolBudgetExceeded as exc:
        reason, error = LIMIT_REACHED, exc
    except InvestigatorError as exc:
        # One error type for an unusable reply and the time budget: tell them apart here.
        reason, error = (LIMIT_REACHED if _wall_clock_spent(state) else None), exc
    except Exception:
        summary("failed")
        log.exception("investigator_failed")
        raise  # the worker marks the run failed
    else:
        result = result_from_draft(draft, run_id, prompt_version, trace)
        summary(result.status.value)
        return result.model_copy(update={"trace": list(trace)})

    log.warning("investigator_escalated", reason=reason or "investigator error",
                error=f"{type(error).__name__}: {error}")
    summary(f"escalated: {reason or 'investigator error'}")
    text = f"{reason} ({error})" if reason else str(error)
    return _escalated(run_id, text, prompt_version, trace)


# ---------- Graph (AGENT_MODE=graph) ----------

def run_graph_mode(req: DisputeRequest, run_id: str) -> RunResult:
    """Investigator and Verifier through the LangGraph graph, over the shared clients."""
    from agent.graph import run_graph

    return run_graph(req, run_id, get_retriever(), get_llm_client())
