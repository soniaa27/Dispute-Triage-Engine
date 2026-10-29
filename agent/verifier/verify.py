"""verify(state, draft, retriever, llm) -> VerifierOutput: the Verifier node's work.

Stages (requirement A3: every answer is checked against retrieved passages):
  a. citations, in code: each cited clause exists (get_clause) and the quoted text is a
     verbatim span of it, at most 60 words (agent/verifier/citations.py);
  b. linked clauses, in code: related() of each cited clause, then get_clause of each link
     not fetched yet, stopping early when the tool budget runs low;
  c. hard rules, in code: always escalate (agent/verifier/rules.py). Run right after stage
     a: when they (or a failed citation) already decide, stage b is skipped;
  d. LLM call 1 "check": is each claim supported by a cited or linked passage, and up to 3
     searches for provisions that could modify or conflict with the draft (run here, k=5);
  e. LLM call 2 "judge", only if those searches found passages not considered yet: does any
     of them modify or conflict with the draft;
  f. the decision: outcome and action (return / retry / escalate).

At most 2 LLM calls (step "verifier") per pass. Errors not handled here propagate to the
graph, which escalates: ToolBudgetExceeded, LLMUnavailableError / LLMGatewayError, and
VerifierError (wall-clock budget spent before a model call). A malformed model reply is
handled here: the draft counts as unsupported and the run escalates.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from agent.tracking import remaining_tool_calls
from agent.verifier.citations import (
    CheckedCitation,
    base_clause_id,
    check_draft_citations,
)
from agent.verifier.prompt import (
    CURRENT_VERIFIER_PROMPT_VERSION,
    VerifierReplyError,
    parse_check,
    parse_judge,
    prompt_file_version,
    render_check,
    render_judge,
)
from agent.verifier.rules import RuleHit, hard_rules, recompute_delay
from contracts.agent import (
    AgentState,
    DocId,
    InvestigatorOutput,
    Passage,
    VerificationOutcome,
    VerifierOutput,
)
from contracts.llm import LLMClient
from contracts.result import Citation, Route
from contracts.tools import Retriever

STEP = "verifier"
SEARCH_K = 5
# Tool calls kept back for the conflict searches while fetching linked clauses.
SEARCH_RESERVE = 3
MAX_LINKED_CLAUSES = 6
# On a boundary delay (D-009) both counting rules are cited, so the analyst sees each side.
COUNTING_RULES = ((DocId.RBI_2017_CIRCULAR, "Table-2-note"), (DocId.IDBI_POLICY, "5.13"))

log = structlog.get_logger("agent.verifier")

_SPACES_AROUND_COLON = re.compile(r"\s*:\s*")


class VerifierError(Exception):
    """The Verifier could not finish its pass; the graph escalates with this message."""


@dataclass
class Verification:
    """verify()'s result plus what the graph needs to cite: the draft's citations that
    passed the check (stage a)."""

    output: VerifierOutput
    citations: list[Citation] = field(default_factory=list)


def _check_deadline(state: AgentState, call: str) -> None:
    elapsed = (datetime.now(UTC) - state.started_at).total_seconds()
    if elapsed >= state.max_wall_clock_seconds:
        raise VerifierError(f"wall-clock budget of {state.max_wall_clock_seconds:g}s spent "
                            f"before the verifier {call} call")


def _split(citation_id: str) -> tuple[DocId, str] | None:
    doc, sep, clause = _SPACES_AROUND_COLON.sub(":", citation_id.strip(), count=1).partition(":")
    try:
        return (DocId(doc), clause) if sep and clause else None
    except ValueError:
        return None


def _key(citation_id: str) -> str | None:
    """Comparable id: "DOC : 5#2" -> "DOC:5"."""
    parts = _split(citation_id)
    return f"{parts[0]}:{base_clause_id(parts[1])}" if parts else None


def _passage_key(passage: Passage) -> str:
    return f"{passage.doc_id}:{base_clause_id(passage.clause_id)}"


def fetch_linked(checked: list[CheckedCitation], retriever: Retriever) -> list[Passage]:
    """Stage b. The clauses linked to the cited ones, each fetched once. Links point both
    ways, so only the cited clauses' links are followed (never a linked clause's own),
    which rules out loops. Stops while SEARCH_RESERVE calls are still left."""
    fetched = {c.citation_id for c in checked}
    linked: list[Passage] = []
    for cited in checked:
        if remaining_tool_calls(retriever) <= SEARCH_RESERVE + 1:  # related + one lookup
            break
        for link in retriever.related(cited.clause.doc_id, cited.citation.clause_id):
            key, parts = _key(link), _split(link)
            if key is None or parts is None or key in fetched:
                continue
            if len(linked) >= MAX_LINKED_CLAUSES or \
                    remaining_tool_calls(retriever) <= SEARCH_RESERVE:
                return linked
            fetched.add(key)
            if passage := retriever.get_clause(parts[0], base_clause_id(parts[1])):
                linked.append(passage)
    return linked


def _fetch_counting_rules(retriever: Retriever, have: set[str]) -> list[Passage]:
    found = []
    for doc_id, clause_id in COUNTING_RULES:
        if f"{doc_id}:{clause_id}" in have or remaining_tool_calls(retriever) < 1:
            continue
        if passage := retriever.get_clause(doc_id, clause_id):
            found.append(passage)
    return found


def _escalate(outcome: VerificationOutcome, reason: str, supported: list[str],
              unsupported: list[str], modifiers: list[Passage] | None = None) -> VerifierOutput:
    return VerifierOutput(outcome=outcome, supported_claims=supported,
                          unsupported_claims=unsupported, modifier_passages=modifiers or [],
                          action="escalate", escalation_reason=reason)


def _retry(unsupported: list[str], supported: list[str]) -> VerifierOutput:
    strategy = ("Re-check these claims against the corpus: for each, retrieve and cite the "
                "specific clause or table row that states it, or drop the claim: "
                + "; ".join(unsupported))
    return VerifierOutput(outcome=VerificationOutcome.unsupported, supported_claims=supported,
                          unsupported_claims=unsupported, action="retry",
                          retry_strategy=strategy)


def decide(*, hits: list[RuleHit], draft: InvestigatorOutput, supported: list[str],
           unsupported: list[str], modifiers: list[Passage]) -> VerifierOutput | None:
    """Stage f for everything decided without a model call; None if the model must check."""
    if hits:
        reason = "; ".join(h.reason for h in hits)
        return _escalate(hits[0].outcome, reason[0].upper() + reason[1:] + ".", supported,
                         unsupported, modifiers)
    if draft.draft_route == Route.escalate:
        return _escalate(VerificationOutcome.unsupported,
                         "The Investigator could not settle liability from the passages and "
                         f"recommends analyst review: {draft.draft_position}",
                         supported, unsupported)
    if unsupported:
        return _retry(unsupported, supported)
    return None


def verify_detailed(state: AgentState, draft: InvestigatorOutput, retriever: Retriever,
                    llm: LLMClient, *, prompt_version: str = CURRENT_VERIFIER_PROMPT_VERSION
                    ) -> Verification:
    started = time.perf_counter()
    request = state.request
    llm_version = prompt_file_version(prompt_version)

    # a. citations
    checked = check_draft_citations(draft, retriever)
    good = [c for c in checked if c.ok]
    citations = [c.citation for c in good]
    supported = [f"{c.citation_id}: quoted text is verbatim in the clause" for c in good]
    unsupported = [f"{c.citation_id}: {c.failure}" for c in checked if not c.ok]

    # c. hard rules (the delay is recomputed, never taken from the draft). Checked before
    # stage b: they do not need the linked clauses, and an escalation should not spend the
    # tool budget on them.
    delay = recompute_delay(request)
    if draft.working_days_delay != delay:
        unsupported.append(f"working-day delay: the draft says {draft.working_days_delay}, "
                           f"recomputed {delay}")
    hits = hard_rules(request, draft, delay, len(good))
    modifiers: list[Passage] = []
    if any(h.rule == "boundary_delay" for h in hits):
        modifiers = _fetch_counting_rules(retriever, {c.citation_id for c in good})

    def done(output: VerifierOutput, **extra) -> Verification:
        log.info("verifier_done", run_id=state.run_id, outcome=output.outcome,
                 action=output.action, rules=[h.rule for h in hits],
                 latency_ms=round((time.perf_counter() - started) * 1000), **extra)
        return Verification(output, citations)

    if decided := decide(hits=hits, draft=draft, supported=supported,
                         unsupported=unsupported, modifiers=modifiers):
        return done(decided, llm_calls=0)

    # b. linked clauses
    linked = fetch_linked(good, retriever)

    # d. LLM call 1: check the claims, propose conflict searches
    shown = [c.clause for c in good] + linked
    shown_keys = {_passage_key(p) for p in shown}
    _check_deadline(state, "check")
    response = llm.complete(render_check(request, draft, delay, shown, prompt_version),
                            prompt_version=llm_version, step=STEP, run_id=state.run_id)
    try:
        check = parse_check(response.text)
    except VerifierReplyError as exc:
        return done(_escalate(VerificationOutcome.unsupported,
                              f"The Verifier's check reply was unusable ({exc}), so the draft "
                              "could not be verified.", supported, unsupported), llm_calls=1)
    for claim in check.claims:
        if claim.supported and _key(claim.clause_id) in shown_keys:
            supported.append(f"{claim.claim} [{_key(claim.clause_id)}]")
        elif claim.supported:
            unsupported.append(f"{claim.claim} (rests on {claim.clause_id or 'no clause'}, "
                               "which is not among the cited or linked passages)")
        else:
            unsupported.append(claim.claim)
    if unsupported:
        return done(_retry(unsupported, supported), llm_calls=1)

    found: dict[str, Passage] = {}
    for query in check.searches:
        for passage in retriever.search_corpus(query, k=SEARCH_K):
            if _passage_key(passage) not in shown_keys:
                found.setdefault(_passage_key(passage), passage)
    new = list(found.values())
    if not new:
        return done(VerifierOutput(outcome=VerificationOutcome.verified,
                                   supported_claims=supported, action="return"), llm_calls=1)

    # e. LLM call 2: do the new passages modify or conflict with the draft?
    _check_deadline(state, "judge")
    response = llm.complete(render_judge(request, draft, delay, shown, new, prompt_version),
                            prompt_version=llm_version, step=STEP, run_id=state.run_id)
    try:
        judge = parse_judge(response.text)
        conflicting = [found[k] for k in dict.fromkeys(map(_key, judge.clause_ids)) if k in found]
        if judge.conflict and not conflicting:
            raise VerifierReplyError("conflict reported without any of the new passages' ids")
    except VerifierReplyError as exc:
        return done(_escalate(VerificationOutcome.unsupported,
                              f"The Verifier's judge reply was unusable ({exc}), so the draft "
                              "could not be verified.", supported, unsupported), llm_calls=2)
    if judge.conflict:
        ids = ", ".join(_passage_key(p) for p in conflicting)
        return done(_escalate(VerificationOutcome.conflicting_provisions,
                              f"Provisions conflict with the draft ({ids}): {judge.reason}",
                              supported, unsupported, conflicting), llm_calls=2)
    return done(VerifierOutput(outcome=VerificationOutcome.verified, supported_claims=supported,
                               action="return"), llm_calls=2)


def verify(state: AgentState, draft: InvestigatorOutput, retriever: Retriever, llm: LLMClient,
           *, prompt_version: str = CURRENT_VERIFIER_PROMPT_VERSION) -> VerifierOutput:
    """The Verifier's judgement on a draft. Reads `state`, never modifies it."""
    return verify_detailed(state, draft, retriever, llm, prompt_version=prompt_version).output

