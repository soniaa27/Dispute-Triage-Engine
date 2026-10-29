"""investigate(state, retriever, llm) -> InvestigatorOutput: the Investigator node's work.

A bounded tool loop (requirement A1: the model chooses the searches). Each turn the model
sees the case facts, the tool calls so far and every passage retrieved, and replies with
either tool requests or the draft. At most MAX_TURNS model calls; the last turn must answer.

Errors are not caught here; Person 2's graph escalates on each (docs/interfaces.md):
- ToolBudgetExceeded from the retriever wrapper (shared tool-call limit)
- LLMUnavailableError / LLMGatewayError from the LLM client ("model unavailable")
- InvestigatorError: malformed reply, no evidence, or the wall-clock budget spent
"""

import re
import time
from datetime import UTC, datetime

import structlog

from agent.investigator.facts import CaseFacts, case_facts
from agent.investigator.prompt import (
    AnswerReply,
    InvestigatorError,
    ReplyFormatError,
    parse_reply,
    render_feedback,
    render_prompt,
)
from agent.investigator.tools import Evidence, run_tools
from agent.prompts import CURRENT_PROMPT_VERSION
from contracts.agent import AgentState, InvestigatorOutput
from contracts.llm import LLMClient
from contracts.tools import Retriever

STEP = "investigator"
# Normally 2 (search, answer); each call waits ~15 s for the gateway's rate limiter.
MAX_TURNS = 3

log = structlog.get_logger("agent.investigator")

_SPACES_AROUND_COLON = re.compile(r"\s*:\s*")


def _check_deadline(state: AgentState, turn: int) -> None:
    elapsed = (datetime.now(UTC) - state.started_at).total_seconds()
    if elapsed >= state.max_wall_clock_seconds:
        raise InvestigatorError(
            f"wall-clock budget of {state.max_wall_clock_seconds:g}s spent before "
            f"investigator turn {turn}")


def investigate(state: AgentState, retriever: Retriever, llm: LLMClient, *,
                prompt_version: str = CURRENT_PROMPT_VERSION,
                max_turns: int = MAX_TURNS) -> InvestigatorOutput:
    """Draft for the Verifier. Reads `state`, never modifies it (the graph owns counters
    and the trace). On a retry, the Verifier's feedback is shown to the model."""
    started = time.perf_counter()
    request = state.request
    facts = case_facts(request)
    previous = state.investigator.draft_position if state.investigator else None
    feedback = render_feedback(state.verifier, previous)
    evidence = Evidence()
    observations: list[str] = []

    for turn in range(1, max_turns + 1):
        _check_deadline(state, turn)
        prompt = render_prompt(request, facts, evidence, observations, turn, max_turns,
                               version=prompt_version, feedback=feedback)
        response = llm.complete(prompt, prompt_version=prompt_version, step=STEP,
                                run_id=state.run_id)
        reply = parse_reply(response.text)
        if isinstance(reply, AnswerReply):
            if evidence.passages:
                return _draft(reply, facts, evidence, state.run_id, turn, started)
            if turn == max_turns:
                raise InvestigatorError("model answered without retrieving any passage")
            observations.append("answer rejected: nothing retrieved yet, search first")
            continue
        if turn == max_turns:
            raise ReplyFormatError("model requested tools on the last turn instead of answering")
        observations += run_tools(retriever, evidence, reply.searches, reply.lookups)
    raise AssertionError("unreachable: the last turn always answers or raises")


def _draft(reply: AnswerReply, facts: CaseFacts, evidence: Evidence, run_id: str, turns: int,
           started: float) -> InvestigatorOutput:
    """Build the output. Cited ids are normalised ("DOC : id" -> "DOC:id"); ids that were
    never retrieved are dropped and logged, never passed on as citations."""
    cited, dropped = [], []
    for raw in reply.cited_clause_ids:
        citation_id = _SPACES_AROUND_COLON.sub(":", raw.strip(), count=1)
        if citation_id in evidence:
            if citation_id not in cited:
                cited.append(citation_id)
        else:
            dropped.append(raw)
    if dropped:
        log.warning("investigator_dropped_citations", run_id=run_id, dropped=dropped)
    log.info("investigator_done", run_id=run_id, turns=turns, route=reply.draft_route,
             passages=len(evidence.passages), cited=len(cited),
             searches=len(evidence.queries_run),
             latency_ms=round((time.perf_counter() - started) * 1000))
    return InvestigatorOutput(
        fraud_type=reply.fraud_type,
        fault_hypothesis=reply.fault_hypothesis,
        working_days_delay=facts.working_days_delay,
        delay_basis=facts.delay_basis,
        passages=evidence.passages,
        queries_run=evidence.queries_run,
        draft_route=reply.draft_route,
        draft_position=reply.draft_position,
        cited_clause_ids=cited,
    )
