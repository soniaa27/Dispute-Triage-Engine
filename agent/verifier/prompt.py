"""Rendering the Verifier's two prompts and parsing the replies strictly.

The text lives in agent/prompts/verifier_<version>.yaml; this module only fills it in. A
reply must be one JSON object with every required field of the right type (true/false must
be JSON booleans); anything else raises VerifierReplyError, which the Verifier treats as an
unsupported draft, never as verified. Unknown extra keys are ignored, as for the
Investigator.
"""

import json
import re
from string import Template

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from agent.prompts import load_prompts
from contracts.agent import InvestigatorOutput, Passage
from contracts.dispute import DisputeRequest

CURRENT_VERIFIER_PROMPT_VERSION = "v1"
MAX_CLAIMS = 8
MAX_SEARCHES = 3
PASSAGE_CHARS = 2000  # as the Investigator: whole tables are cut, their rows are passages

_COMPLAINT_TAG = re.compile(r"<\s*/?\s*complaint\s*>", re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class VerifierReplyError(ValueError):
    """The model's reply is not valid JSON of the expected shape."""


class ClaimCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim: str = Field(min_length=1, max_length=500)
    supported: StrictBool
    clause_id: str = Field(max_length=80)


class CheckReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claims: list[ClaimCheck] = Field(min_length=1)
    searches: list[str] = Field(min_length=1)


class JudgeReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conflict: StrictBool
    clause_ids: list[str]
    reason: str = Field(min_length=1, max_length=500)


def prompt_file_version(version: str) -> str:
    """agent/prompts/verifier_v1.yaml declares version "verifier_v1"."""
    return f"verifier_{version}"


def _templates(version: str) -> dict[str, str]:
    return load_prompts(prompt_file_version(version))["agents"]["verifier"]


def _parse(text: str, model: type[BaseModel]) -> BaseModel:
    body = _FENCE.sub("", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise VerifierReplyError(f"no JSON object in reply: {text[:120]!r}")
    try:
        return model.model_validate(json.loads(body[start:end + 1]))
    except json.JSONDecodeError as exc:
        raise VerifierReplyError(f"reply is not valid JSON: {exc}") from exc
    except ValidationError as exc:
        fields = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise VerifierReplyError(f"reply does not match the schema: {fields}") from exc


def parse_check(text: str) -> CheckReply:
    reply = _parse(text, CheckReply)
    if len(reply.claims) > MAX_CLAIMS:
        raise VerifierReplyError(f"reply lists {len(reply.claims)} claims, at most "
                                 f"{MAX_CLAIMS} allowed")
    queries = [q.strip() for q in reply.searches if q.strip()]
    if not queries:
        raise VerifierReplyError("reply gives no search query")
    # Extra queries are dropped, not fatal: the tool budget, not the reply, is the limit.
    return reply.model_copy(update={"searches": queries[:MAX_SEARCHES]})


def parse_judge(text: str) -> JudgeReply:
    return _parse(text, JudgeReply)


def _claim(value: bool | None) -> str:
    return "not stated" if value is None else ("yes" if value else "no")


def render_facts(request: DisputeRequest, delay: int | None) -> str:
    claims = request.customer_claims
    alert = request.bank_alert_datetime.isoformat() if request.bank_alert_datetime else \
        "not given"
    return "\n".join([
        f"- Account type: {request.account_type}",
        f"- Channel: {request.channel}",
        f"- Amount: Rs {request.transaction_amount_inr:,.2f}",
        f"- Bank alert sent: {alert}",
        f"- Reported to the bank: {request.reported_datetime.isoformat()}",
        f"- Working-day delay (computed): {'unknown' if delay is None else delay}",
        f"- Customer says they shared an OTP or PIN: {_claim(claims.shared_otp_or_pin)}",
        f"- Customer says the card was in their possession: "
        f"{_claim(claims.card_in_possession)}",
        f"- Customer says they received the alert: {_claim(claims.received_alert)}",
    ])


def render_draft(draft: InvestigatorOutput) -> str:
    return "\n".join([
        f"- Fraud type: {draft.fraud_type}",
        f"- Fault: {draft.fault_hypothesis}",
        f"- Route: {draft.draft_route}",
        f"- Position: {draft.draft_position}",
        f"- Delay basis: {draft.delay_basis}",
        f"- Cites: {', '.join(draft.cited_clause_ids) or 'nothing'}",
    ])


def render_passages(passages: list[Passage]) -> str:
    blocks = []
    for passage in passages:
        text = passage.text
        if len(text) > PASSAGE_CHARS:
            text = text[:PASSAGE_CHARS] + " ... (truncated)"
        blocks.append(f"[{passage.citation_id}]\n{text}")
    return "\n\n".join(blocks) or "none"


def render_check(request: DisputeRequest, draft: InvestigatorOutput, delay: int | None,
                 passages: list[Passage], version: str = CURRENT_VERIFIER_PROMPT_VERSION
                 ) -> str:
    return Template(_templates(version)["check"]).substitute(
        max_claims=MAX_CLAIMS, max_searches=MAX_SEARCHES,
        facts=render_facts(request, delay),
        complaint=_COMPLAINT_TAG.sub("[tag removed]", request.complaint_text),
        draft=render_draft(draft), passages=render_passages(passages),
    )


def render_judge(request: DisputeRequest, draft: InvestigatorOutput, delay: int | None,
                 relied_on: list[Passage], new_passages: list[Passage],
                 version: str = CURRENT_VERIFIER_PROMPT_VERSION) -> str:
    return Template(_templates(version)["judge"]).substitute(
        facts=render_facts(request, delay), draft=render_draft(draft),
        relied_on=render_passages(relied_on), new_passages=render_passages(new_passages),
    )
