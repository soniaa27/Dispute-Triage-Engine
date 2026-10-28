"""Rendering the Investigator's prompt and parsing the model's reply.

The prompt text lives in agent/prompts/<version>.yaml; this module only fills it in. The
reply must be one JSON object: a tool request ("search") or the draft ("answer"). Anything
else raises ReplyFormatError. There is no repair call: at ~4 LLM calls a minute a second
attempt would cost the run's time budget, and the graph escalates instead.
"""

import json
import re
from string import Template
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agent.investigator.facts import CaseFacts
from agent.investigator.tools import (
    MAX_LOOKUPS_PER_TURN,
    MAX_SEARCHES_PER_TURN,
    Evidence,
    LookupRequest,
    SearchRequest,
)
from agent.prompts import CURRENT_PROMPT_VERSION, agent_prompts
from contracts.agent import FaultHypothesis, VerifierOutput
from contracts.dispute import DisputeRequest
from contracts.result import FraudType, Route

# Longest passage text shown to the model; whole tables (~2,600 chars) are cut, their rows
# are separate passages anyway.
PASSAGE_CHARS = 2000
MAX_POSITION_CHARS = 2000

_COMPLAINT_TAG = re.compile(r"<\s*/?\s*complaint\s*>", re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class InvestigatorError(Exception):
    """The Investigator could not produce a draft; the graph escalates with this message."""


class ReplyFormatError(InvestigatorError, ValueError):
    """The model's reply is not a valid search request or draft."""


class SearchReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["search"]
    searches: list[SearchRequest] = Field(default_factory=list)
    lookups: list[LookupRequest] = Field(default_factory=list)
    reason: str | None = None


class AnswerReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["answer"]
    fraud_type: FraudType
    fault_hypothesis: FaultHypothesis
    draft_route: Route
    draft_position: str = Field(min_length=1, max_length=MAX_POSITION_CHARS)
    cited_clause_ids: list[str] = Field(default_factory=list)


Reply = SearchReply | AnswerReply
_REPLY = TypeAdapter(Annotated[Reply, Field(discriminator="action")])


def parse_reply(text: str) -> Reply:
    body = _FENCE.sub("", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise ReplyFormatError(f"no JSON object in reply: {text[:120]!r}")
    try:
        reply = _REPLY.validate_python(json.loads(body[start:end + 1]))
    except json.JSONDecodeError as exc:
        raise ReplyFormatError(f"reply is not valid JSON: {exc}") from exc
    except ValidationError as exc:
        fields = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ReplyFormatError(f"reply does not match the schema: {fields}") from exc
    if isinstance(reply, SearchReply) and not (reply.searches or reply.lookups):
        raise ReplyFormatError("search reply requests no searches or lookups")
    return reply


def _claim(value: bool | None) -> str:
    return "not stated" if value is None else ("yes" if value else "no")


def render_facts(request: DisputeRequest, facts: CaseFacts) -> str:
    claims = request.customer_claims
    alert = request.bank_alert_datetime.isoformat() if request.bank_alert_datetime else \
        "not given"
    delay = "unknown" if facts.working_days_delay is None else str(facts.working_days_delay)
    return "\n".join([
        f"- Account type: {request.account_type}",
        f"- Channel: {request.channel}",
        f"- Amount: Rs {request.transaction_amount_inr:,.2f}",
        f"- Transaction time: {request.transaction_datetime.isoformat()}",
        f"- Bank alert sent: {alert}",
        f"- Reported to the bank: {request.reported_datetime.isoformat()}",
        f"- Home branch state: {request.home_branch_state}",
        f"- Customer says they shared an OTP or PIN: {_claim(claims.shared_otp_or_pin)}",
        f"- Customer says the card was in their possession: "
        f"{_claim(claims.card_in_possession)}",
        f"- Customer says they received the alert: {_claim(claims.received_alert)}",
        f"- Working-day delay (computed): {delay}",
        f"- How it was computed: {facts.delay_basis}",
        f"- Calendar days to the report: {facts.calendar_days}",
        f"- Delay on a table boundary: {'yes' if facts.on_boundary else 'no'}",
    ])


def render_passages(evidence: Evidence) -> str:
    blocks = []
    for passage in evidence.passages:
        text = passage.text
        if len(text) > PASSAGE_CHARS:
            text = text[:PASSAGE_CHARS] + " ... (truncated)"
        page = f" (page {passage.page})" if passage.page else ""
        blocks.append(f"[{passage.citation_id}]{page}\n{text}")
    return "\n\n".join(blocks) or "none yet"


def render_feedback(verifier: VerifierOutput | None, previous_position: str | None) -> str:
    if verifier is None or verifier.action != "retry":
        return "none (first draft)"
    lines = [f"- Outcome: {verifier.outcome}", f"- Retry strategy: {verifier.retry_strategy}"]
    lines += [f"- Unsupported claim: {claim}" for claim in verifier.unsupported_claims]
    lines += [f"- Provision to consider: [{p.citation_id}]" for p in verifier.modifier_passages]
    if previous_position:
        lines.append(f"- Previous draft position: {previous_position}")
    return "\n".join(lines)


def render_prompt(request: DisputeRequest, facts: CaseFacts, evidence: Evidence,
                  observations: list[str], turn: int, max_turns: int,
                  version: str = CURRENT_PROMPT_VERSION, feedback: str = "none (first draft)"
                  ) -> str:
    templates = agent_prompts("investigator", version)
    if turn >= max_turns:
        instruction = "This is the last turn: answer now, using only the passages above."
    elif not evidence.passages:
        instruction = "Nothing has been retrieved yet: request searches first."
    else:
        instruction = "Request more tools only if the passages above do not settle the case."
    system = Template(templates["system"]).substitute(
        max_searches=MAX_SEARCHES_PER_TURN, max_lookups=MAX_LOOKUPS_PER_TURN)
    turn_text = Template(templates["turn"]).substitute(
        facts=render_facts(request, facts),
        complaint=_COMPLAINT_TAG.sub("[tag removed]", request.complaint_text),
        feedback=feedback,
        observations="\n".join(observations) or "none yet",
        passages=render_passages(evidence),
        turn=turn, max_turns=max_turns, turn_instruction=instruction,
    )
    return f"{system}\nCASE\n{turn_text}"
