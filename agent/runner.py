"""Agent entry point called by the worker: run_agent(req, run_id, mode) -> RunResult.

AGENT_MODE (worker settings) picks what runs:
- fake: deterministic placeholder after a fixed delay, no model or retrieval ("fake-0").
- investigator: the Investigator alone; its draft is returned unverified (prompt_version
  "<version>+unverified"). TEMPORARY direct call: the LangGraph graph with the Verifier
  replaces run_investigator. Keep the signature and the RunResult contract unchanged so the
  worker and API need no edits.

In investigator mode a run never returns a confident answer it did not finish: the model
being unavailable, the tool or time budget running out, or an unusable model reply all
escalate to an analyst; any other exception propagates and the worker marks the run failed.
"""

import time
from collections.abc import Callable, Sequence
from functools import cache
from typing import Any, Literal

import structlog

from agent.errors import ToolBudgetExceeded
from agent.investigator import InvestigatorError, investigate
from agent.llm_client import LLMGatewayError
from agent.prompts import CURRENT_PROMPT_VERSION
from contracts import Channel, Citation, DisputeRequest, FraudType, Route, RunResult, RunStatus
from contracts.agent import AgentState, DocId, InvestigatorOutput, Passage
from contracts.llm import LLMClient, LLMResponse, LLMUnavailableError
from contracts.tools import Retriever

AgentMode = Literal["fake", "investigator"]

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

MODEL_UNAVAILABLE = "model unavailable"
LIMIT_REACHED = "investigation limit reached"


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


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


class TracedRetriever:
    """The Retriever the Investigator sees: counts every call against the run's tool-call
    limit, raising ToolBudgetExceeded instead of making the call over it, and records each
    call in the trace with its latency."""

    def __init__(self, inner: Retriever, trace: list[dict[str, Any]], max_calls: int):
        self._inner = inner
        self._trace = trace
        self._max_calls = max_calls
        self.calls = 0

    def _call(self, entry: dict[str, Any], call: Callable[[], Any]) -> Any:
        if self.calls >= self._max_calls:
            raise ToolBudgetExceeded(f"tool-call limit of {self._max_calls} reached")
        self.calls += 1
        started = time.perf_counter()
        try:
            return call()
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["latency_ms"] = _ms(started)
            self._trace.append(entry)

    def search_corpus(self, query: str, k: int = 5,
                      doc_ids: Sequence[DocId] | None = None) -> list[Passage]:
        entry: dict[str, Any] = {"step": "search_corpus", "query": query, "k": k}
        if doc_ids:
            entry["doc_ids"] = [str(d) for d in doc_ids]
        found = self._call(entry, lambda: self._inner.search_corpus(query, k=k, doc_ids=doc_ids))
        entry["results"] = [p.citation_id for p in found]
        return found

    def get_clause(self, doc_id: DocId, clause_id: str) -> Passage | None:
        entry: dict[str, Any] = {"step": "get_clause", "clause": f"{doc_id}:{clause_id}"}
        passage = self._call(entry, lambda: self._inner.get_clause(doc_id, clause_id))
        entry["found"] = passage is not None
        return passage

    def related(self, doc_id: DocId, clause_id: str) -> list[str]:
        entry: dict[str, Any] = {"step": "related", "clause": f"{doc_id}:{clause_id}"}
        links = self._call(entry, lambda: self._inner.related(doc_id, clause_id))
        entry["results"] = links
        return links


class TracedLLM:
    """The LLMClient the Investigator sees: records each call (tokens, cost, latency)."""

    def __init__(self, inner: LLMClient, trace: list[dict[str, Any]]):
        self._inner = inner
        self._trace = trace
        self.calls = 0

    def complete(self, prompt: str, *, prompt_version: str, step: str, run_id: str,
                 max_tokens: int | None = None) -> LLMResponse:
        self.calls += 1
        entry: dict[str, Any] = {"step": "llm", "agent": step, "prompt_version": prompt_version}
        started = time.perf_counter()
        try:
            response = self._inner.complete(prompt, prompt_version=prompt_version, step=step,
                                            run_id=run_id, max_tokens=max_tokens)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["latency_ms"] = _ms(started)
            self._trace.append(entry)
        entry.update(model=response.model, prompt_tokens=response.prompt_tokens,
                     completion_tokens=response.completion_tokens, cost_usd=response.cost_usd)
        return response


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
                      "tool_calls": retriever.calls, "latency_ms": _ms(started)})

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
