"""The LangGraph graph: Investigator and Verifier with a bounded retry loop (A1, A2, A3).

    START -> investigate -> verify --return-----> finalize -> END
                ^             |----escalate----->    ^
                |             |                      |
                +---retry-----+ (at most AGENT_MAX_VERIFY_RETRIES, then escalate)
             a node that stops the run (limit, model unavailable, agent error) -> finalize

One TracedRetriever and one TracedLLM per run are shared by both agents: one tool-call
budget (AGENT_MAX_TOOL_CALLS) and one trace. Before each agent node the graph checks the
step limit (AGENT_MAX_STEPS) and the wall clock (AGENT_MAX_WALL_CLOCK_SECONDS).

Failures inside a node stop the run and escalate, never answer:
- LLMUnavailableError / LLMGatewayError -> "model unavailable (...)"
- ToolBudgetExceeded, the step limit or the wall clock -> "investigation limit reached (...)"
- InvestigatorError / VerifierError -> their message (a time limit counts as a limit)
- anything else propagates: the worker marks the run failed.
A run returns a confident answer (status completed) only when the Verifier's action is
"return", which VerifierOutput allows only for outcome verified with nothing unsupported.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from langgraph.graph import END, START, StateGraph
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.errors import LIMIT_REACHED, MODEL_UNAVAILABLE, ToolBudgetExceeded
from agent.investigator import InvestigatorError, investigate
from agent.llm_client import LLMGatewayError
from agent.prompts import CURRENT_PROMPT_VERSION
from agent.tracking import TracedLLM, TracedRetriever, ms_since
from agent.verifier import CURRENT_VERIFIER_PROMPT_VERSION, VerifierError, verify_detailed
from agent.verifier.citations import to_citation
from contracts import Citation, DisputeRequest, Route, RunResult, RunStatus
from contracts.agent import AgentState, VerificationOutcome, VerifierOutput
from contracts.llm import LLMClient, LLMUnavailableError
from contracts.tools import Retriever

log = structlog.get_logger("agent.graph")

Node = Literal["investigate", "verify", "finalize"]


class GraphSettings(BaseSettings):
    """Graph settings beyond AgentLimits, from AGENT_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    # Times the Investigator may redraft after the Verifier finds unsupported claims.
    max_verify_retries: int = Field(default=1, ge=0)


class GraphState(AgentState):
    """AgentState plus what the graph itself needs between nodes."""

    halt_reason: str | None = None  # set by a node that stopped the run; finalize escalates
    verified_citations: list[Citation] = Field(default_factory=list)
    result: RunResult | None = None


@dataclass
class RunContext:
    """Per-run tools, shared by every node: one budget, one trace."""

    retriever: TracedRetriever
    llm: TracedLLM
    trace: list[dict[str, Any]]
    max_verify_retries: int
    investigator_version: str = CURRENT_PROMPT_VERSION
    verifier_version: str = CURRENT_VERIFIER_PROMPT_VERSION

    @property
    def prompt_version(self) -> str:
        return f"inv-{self.investigator_version}+ver-{self.verifier_version}"


def _elapsed(state: AgentState) -> float:
    return (datetime.now(UTC) - state.started_at).total_seconds()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _halt_reason(exc: Exception, state: AgentState) -> str | None:
    """The escalation reason for an error that stops the run; None if it is not one."""
    if isinstance(exc, LLMUnavailableError | LLMGatewayError):
        return f"{MODEL_UNAVAILABLE} ({exc})"
    if isinstance(exc, ToolBudgetExceeded):
        return f"{LIMIT_REACHED} ({exc})"
    if isinstance(exc, InvestigatorError | VerifierError):
        # One error type for an unusable reply and the time budget: tell them apart here.
        return f"{LIMIT_REACHED} ({exc})" if _elapsed(state) >= state.max_wall_clock_seconds \
            else str(exc)
    return None


def _agent_node(ctx: RunContext, name: Node,
                body: Callable[[GraphState], dict[str, Any]]) -> Callable[[GraphState], dict]:
    """Wraps an agent node: limits before it, errors to escalation, timing to the trace."""

    def run(state: GraphState) -> dict[str, Any]:
        step = state.step_count + 1
        entry: dict[str, Any] = {"step": "node", "node": name, "attempt": state.attempts + 1,
                                 "started_at": _now()}
        started = time.perf_counter()
        update: dict[str, Any] = {"step_count": step}
        try:
            if step > state.max_steps:
                update["halt_reason"] = (f"{LIMIT_REACHED} (step limit of {state.max_steps} "
                                         "reached)")
            elif _elapsed(state) >= state.max_wall_clock_seconds:
                update["halt_reason"] = (f"{LIMIT_REACHED} (wall-clock budget of "
                                         f"{state.max_wall_clock_seconds:g}s spent before "
                                         f"{name})")
            else:
                update |= body(state)
        except Exception as exc:
            reason = _halt_reason(exc, state)
            entry.update(outcome="failed" if reason is None else "escalated",
                         error=f"{type(exc).__name__}: {exc}")
            if reason is None:
                log.exception("agent_node_failed", run_id=state.run_id, node=name)
                raise
            log.warning("agent_node_escalated", run_id=state.run_id, node=name, reason=reason)
            update["halt_reason"] = reason
        finally:
            entry.setdefault("outcome", "halted" if update.get("halt_reason") else "ok")
            entry.update(ended_at=_now(), latency_ms=ms_since(started))
            ctx.trace.append(entry)
        update.update(trace=list(ctx.trace), tool_call_count=ctx.retriever.calls)
        return update

    return run


def _investigate(ctx: RunContext, state: GraphState) -> dict[str, Any]:
    draft = investigate(state, ctx.retriever, ctx.llm, prompt_version=ctx.investigator_version)
    ctx.trace.append({"step": "investigator_draft", "attempt": state.attempts + 1,
                      "fraud_type": str(draft.fraud_type), "fault": str(draft.fault_hypothesis),
                      "route": str(draft.draft_route), "cited": draft.cited_clause_ids})
    return {"investigator": draft}


def _verify(ctx: RunContext, state: GraphState) -> dict[str, Any]:
    if state.investigator is None:
        raise VerifierError("no Investigator draft to verify")
    verification = verify_detailed(state, state.investigator, ctx.retriever, ctx.llm,
                                   prompt_version=ctx.verifier_version)
    output = verification.output
    attempts = state.attempts
    if output.action == "retry":
        if attempts >= ctx.max_verify_retries:
            output = _retries_exhausted(output, ctx.max_verify_retries)
        else:
            attempts += 1
    reason = output.escalation_reason or output.retry_strategy or "all claims supported"
    ctx.trace.append({"step": "verifier_decision", "attempt": state.attempts + 1,
                      "outcome": str(output.outcome), "action": output.action, "reason": reason,
                      "supported": len(output.supported_claims),
                      "unsupported": output.unsupported_claims,
                      "modifiers": [p.citation_id for p in output.modifier_passages]})
    return {"verifier": output, "attempts": attempts,
            "verified_citations": verification.citations}


def _retries_exhausted(output: VerifierOutput, retries: int) -> VerifierOutput:
    tries = f"{retries} retr{'y' if retries == 1 else 'ies'}"
    return output.model_copy(update={
        "action": "escalate", "retry_strategy": None,
        "escalation_reason": (f"Claims still unsupported after {tries}: "
                              + "; ".join(output.unsupported_claims)),
    })


def _after_investigate(state: GraphState) -> Node:
    return "finalize" if state.halt_reason else "verify"


def _after_verify(state: GraphState) -> Node:
    if state.halt_reason or state.verifier is None:
        return "finalize"
    return "investigate" if state.verifier.action == "retry" else "finalize"


def _citations(*groups: list[Citation]) -> list[Citation]:
    seen, out = set(), []
    for citation in (c for group in groups for c in group):
        if (key := (citation.doc_id, citation.clause_id)) not in seen:
            seen.add(key)
            out.append(citation)
    return out


def result_from_state(state: GraphState, ctx: RunContext,
                      trace: list[dict[str, Any]]) -> RunResult:
    """Map the final state onto RunResult. Completed only on the Verifier's "return"."""
    common = dict(run_id=state.run_id, prompt_version=ctx.prompt_version, trace=trace)
    draft, verdict = state.investigator, state.verifier
    if state.halt_reason or draft is None or verdict is None:
        return RunResult(status=RunStatus.escalated, route=Route.escalate,
                         escalation_reason=state.halt_reason or "verification did not complete",
                         **common)
    relied_on = ", ".join(f"{c.doc_id}:{c.clause_id}" for c in state.verified_citations)
    if verdict.action == "return":
        basis = (f"Fault hypothesis: {draft.fault_hypothesis}. Reporting delay: "
                 f"{draft.delay_basis} Clauses relied on: {relied_on}. Verified by a second "
                 f"agent: {len(verdict.supported_claims)} claims checked against the cited and "
                 "linked passages; no modifying or conflicting provision found.")
        return RunResult(status=RunStatus.completed, route=draft.draft_route,
                         fraud_type=draft.fraud_type, liability_position=draft.draft_position,
                         basis=basis, citations=state.verified_citations, **common)
    # Escalated: cite the draft's verified provisions and every modifying or conflicting one,
    # so the analyst sees both sides.
    modifiers = [to_citation(p) for p in verdict.modifier_passages]
    other_side = ", ".join(f"{c.doc_id}:{c.clause_id}" for c in modifiers)
    basis = (f"Verifier outcome: {verdict.outcome}. Investigator draft, not adopted: route "
             f"{draft.draft_route}, fault {draft.fault_hypothesis}. {draft.draft_position} "
             f"Reporting delay: {draft.delay_basis} Clauses relied on: {relied_on or 'none'}."
             + (f" Modifying or conflicting provisions: {other_side}." if modifiers else ""))
    position = "not_applicable" if verdict.outcome == VerificationOutcome.out_of_scope \
        else "undetermined"
    return RunResult(status=RunStatus.escalated, route=Route.escalate,
                     fraud_type=draft.fraud_type, liability_position=position, basis=basis,
                     citations=_citations(state.verified_citations, modifiers),
                     escalation_reason=verdict.escalation_reason, **common)


def _finalize(ctx: RunContext) -> Callable[[GraphState], dict]:
    def run(state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        entry: dict[str, Any] = {"step": "node", "node": "finalize", "started_at": _now()}
        draft, verdict = state.investigator, state.verifier
        outcome = ("escalated" if state.halt_reason or verdict is None or draft is None
                   or verdict.action != "return" else "completed")
        ctx.trace.append({"step": "graph", "outcome": outcome, "llm_calls": ctx.llm.calls,
                          "tool_calls": ctx.retriever.calls, "steps": state.step_count,
                          "attempts": state.attempts + 1, "elapsed_s": round(_elapsed(state), 1)})
        entry.update(ended_at=_now(), latency_ms=ms_since(started), outcome=outcome)
        ctx.trace.append(entry)
        result = result_from_state(state, ctx, list(ctx.trace))
        return {"result": result, "trace": list(ctx.trace)}

    return run


def build_graph(ctx: RunContext):
    graph = StateGraph(GraphState)
    graph.add_node("investigate", _agent_node(ctx, "investigate",
                                              lambda s: _investigate(ctx, s)))
    graph.add_node("verify", _agent_node(ctx, "verify", lambda s: _verify(ctx, s)))
    graph.add_node("finalize", _finalize(ctx))
    graph.add_edge(START, "investigate")
    graph.add_conditional_edges("investigate", _after_investigate, ["verify", "finalize"])
    graph.add_conditional_edges("verify", _after_verify, ["investigate", "finalize"])
    graph.add_edge("finalize", END)
    return graph.compile()


def run_graph(req: DisputeRequest, run_id: str, retriever: Retriever, llm: LLMClient, *,
              settings: GraphSettings | None = None, **limits: Any) -> RunResult:
    """One run through the graph. `limits` (max_steps, max_tool_calls,
    max_wall_clock_seconds) override AGENT_MAX_*."""
    settings = settings or GraphSettings()
    state = GraphState(request=req, run_id=run_id, **limits)
    trace: list[dict[str, Any]] = []
    ctx = RunContext(retriever=TracedRetriever(retriever, trace, state.max_tool_calls),
                     llm=TracedLLM(llm, trace), trace=trace,
                     max_verify_retries=settings.max_verify_retries)
    # The step limit is enforced in the nodes; LangGraph's own limit is only a backstop.
    final = build_graph(ctx).invoke(state, {"recursion_limit": state.max_steps + 5})
    result: RunResult = final["result"]
    log.info("graph_done", run_id=run_id, status=result.status, route=result.route,
             llm_calls=ctx.llm.calls, tool_calls=ctx.retriever.calls)
    return result
