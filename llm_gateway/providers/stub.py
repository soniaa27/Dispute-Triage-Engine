"""Stub provider: a simulated model for development and load tests (Locust), with no key,
quota or cost. Every call waits STUB_DELAY_MS (docs/slo.md assumes 4 s per call).

Agent calls get a reply in the format the agent parses, so a stub-driven run goes through
the whole graph (docs/load_test.md):

- Which call: the gateway binds the request's `step` and `prompt_version` (the values stored
  in llm_calls); the stub reads them and tells the calls apart by the prompt's own headers.
    investigator, v<n>     turn 1 ("This is turn 1 of ..."): look up the case's clauses;
                           a later turn with RETRIEVED PASSAGES: the draft
    verifier, verifier_v<n> "PASSAGES (cited by the draft ...": the check (every cited
                           claim supported, one conflict search); "NEW PASSAGES": the
                           judge (no conflict)
- Which case: the eval scenario whose amount and report time appear in the CASE FACTS
  (eval/scenarios.jsonl, copied into the gateway image). The draft is the scenario's
  expected fraud type, fault, route and supporting clauses, so clear-cut cases are verified
  and complete, and the must-escalate ones escalate (the Investigator drafts "escalate",
  or a Verifier hard rule fires). An unknown case gets an "unclear / escalate" draft.

Anything else (another prompt version, the eval judge, a prompt without these headers) gets
the fixed STUB_TEXT, as the stub always returned. Replies are deterministic.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog

from llm_gateway.providers.base import ProviderResult

STUB_TEXT = '{"stub": true, "note": "fixed response from llm-gateway stub mode"}'
STUB_MODEL = "stub"
DEFAULT_SCENARIOS = Path("eval/scenarios.jsonl")
MAX_LOOKUPS = 4  # agent/investigator/tools.py MAX_LOOKUPS_PER_TURN
MAX_CLAIMS = 8   # agent/verifier/prompt.py MAX_CLAIMS

_INVESTIGATOR_VERSION = re.compile(r"v\d+")
_VERIFIER_VERSION = re.compile(r"verifier_v\d+")
_TURN = re.compile(r"This is turn (\d+) of (\d+)")
_PASSAGE_ID = re.compile(r"^\[([A-Z0-9_]+:[^\]\s]+)\]", re.MULTILINE)
_LINE = re.compile(r"^- (\w[\w ]*): (.*)$", re.MULTILINE)
_CHECK_HEADER = "PASSAGES (cited by the draft, then the clauses they link to)"
_JUDGE_HEADER = "NEW PASSAGES"

log = structlog.get_logger("llm_gateway.stub")


def _approx_tokens(text: str) -> int:
    # Rough 4-chars-per-token estimate. Only the stub estimates; real providers report usage.
    return max(1, len(text) // 4)


def _section(prompt: str, header: str, end: str) -> str:
    start = prompt.rfind(f"{header}\n")
    if start < 0:
        return ""
    body = prompt[start + len(header) + 1:]
    stop = body.find(end)
    return body if stop < 0 else body[:stop]


@dataclass(frozen=True)
class Case:
    scenario_id: str
    fraud_type: str
    fault: str
    route: str
    liability: str
    clauses: tuple[str, ...]


def _fingerprint(request: dict) -> tuple[str, str]:
    """Lines both agents' CASE FACTS contain, unique per scenario."""
    reported = datetime.fromisoformat(request["reported_datetime"]).isoformat()
    return (f"- Amount: Rs {request['transaction_amount_inr']:,.2f}",
            f"- Reported to the bank: {reported}")


class AgentReplies:
    """Replies for the Investigator and the Verifier, from the eval scenarios."""

    def __init__(self, scenarios: list[dict]):
        self.cases: dict[tuple[str, str], Case] = {}
        for s in scenarios:
            e = s["expected"]
            self.cases[_fingerprint(s["request"])] = Case(
                s["id"], e["fraud_type"], e["fault_hypothesis"], e["route"],
                e.get("customer_liability") or "undetermined", tuple(e["supporting_clauses"]))

    @classmethod
    def load(cls, path: Path = DEFAULT_SCENARIOS) -> "AgentReplies":
        if not path.is_file():
            log.warning("stub_scenarios_missing", path=str(path))
            return cls([])
        with path.open(encoding="utf-8") as f:
            return cls([json.loads(line) for line in f if line.strip()])

    def case(self, prompt: str) -> Case | None:
        return next((c for key, c in self.cases.items() if all(k in prompt for k in key)), None)

    def reply(self, prompt: str, step: str | None, prompt_version: str | None) -> str | None:
        version = prompt_version or ""
        if step == "investigator" and _INVESTIGATOR_VERSION.fullmatch(version):
            return self._investigator(prompt)
        if step == "verifier" and _VERIFIER_VERSION.fullmatch(version):
            if _CHECK_HEADER in prompt:
                return self._check(prompt)
            if f"\n{_JUDGE_HEADER}\n" in prompt:
                return self._judge()
        return None

    def _investigator(self, prompt: str) -> str | None:
        turn = _TURN.search(prompt)
        if turn is None:
            return None
        passages = _PASSAGE_ID.findall(_section(prompt, "RETRIEVED PASSAGES", "This is turn"))
        case = self.case(prompt)
        last_turn = int(turn.group(1)) >= int(turn.group(2))
        if not passages and not last_turn:
            if case is None:
                return json.dumps({"action": "search", "lookups": [], "searches": [{
                    "query": "customer liability unauthorised electronic banking transaction"}],
                    "reason": "stub: unknown case"})
            lookups = [dict(zip(("doc_id", "clause_id"), c.split(":", 1), strict=False))
                       for c in case.clauses[:MAX_LOOKUPS]]
            return json.dumps({"action": "search", "searches": [], "lookups": lookups,
                               "reason": f"stub: clauses of {case.scenario_id}"})
        if case is None:
            return json.dumps({
                "action": "answer", "fraud_type": "unknown", "fault_hypothesis": "unclear",
                "draft_route": "escalate", "cited_clause_ids": passages[:1],
                "draft_position": "Stub: the case is not one of the evaluation scenarios, so "
                                  "liability is undetermined and an analyst should decide."})
        cited = [p for p in passages if p in case.clauses] or passages[:1]
        return json.dumps({
            "action": "answer", "fraud_type": case.fraud_type, "fault_hypothesis": case.fault,
            "draft_route": case.route, "cited_clause_ids": cited,
            "draft_position": f"Customer liability: {case.liability}; fault: {case.fault}; "
                              f"route: {case.route} [{', '.join(cited)}]."})

    def _check(self, prompt: str) -> str:
        shown = _PASSAGE_ID.findall(_section(prompt, _CHECK_HEADER, "\nREPLY"))
        draft = dict(_LINE.findall(_section(prompt, "DRAFT", "\nPASSAGES")))
        cited = [c.strip() for c in draft.get("Cites", "").split(",") if c.strip() in shown]
        claims = [{"claim": f"The draft's position rests on {c}.", "supported": True,
                   "clause_id": c} for c in cited[:MAX_CLAIMS]]
        if not claims:
            claims = [{"claim": "The draft's position.", "supported": False, "clause_id": ""}]
        other_reading = ("third party breach zero liability reported within three working days"
                         if draft.get("Fault") == "customer_negligence"
                         else "customer negligence full liability before reporting")
        return json.dumps({"claims": claims, "searches": [other_reading]})

    @staticmethod
    def _judge() -> str:
        return json.dumps({"conflict": False, "clause_ids": [],
                           "reason": "Stub: the new passages agree with the draft."})


class StubProvider:
    name = "stub"

    def __init__(self, delay_ms: int, replies: AgentReplies | None = None):
        self.delay_ms = delay_ms
        self.replies = replies or AgentReplies([])

    async def generate(self, prompt, *, model, max_tokens, temperature, timeout_s):
        await asyncio.sleep(self.delay_ms / 1000)
        # Bound by the /v1/complete handler for this request.
        bound = structlog.contextvars.get_contextvars()
        text = self.replies.reply(prompt, bound.get("step"), bound.get("prompt_version")) \
            or STUB_TEXT
        return ProviderResult(
            text=text,
            prompt_tokens=_approx_tokens(prompt),
            completion_tokens=_approx_tokens(text),
            model=STUB_MODEL,
            raw_finish_reason="STOP",
        )
