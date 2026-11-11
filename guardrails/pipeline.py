"""guard_input and guard_output: what agent.runner.run_agent calls around every run.

    request --guard_input--> sanitised request --agent--> RunResult --guard_output--> stored

guard_input  removes injected instructions (injection.py) and masks profanity and insults
             (toxicity.py) in complaint_text. A threat or self-harm statement blocks the
             run: it is escalated to an analyst without calling the agents.
guard_output applies the output filter (output.py) and adds one "guardrails" entry to the
             trace with everything both checks found, so an analyst and the eval see it.

Neither raises on a dispute: a guardrail can only make a run more conservative (remove text,
escalate), never make it answer.
"""

from dataclasses import dataclass, field
from typing import Any

import structlog

from contracts import DisputeRequest, Route, RunResult, RunStatus
from guardrails.injection import detect_injection
from guardrails.output import check_output
from guardrails.toxicity import check_toxicity

log = structlog.get_logger("guardrails")

GUARDRAIL_STEP = "guardrails"
BLOCKED_PROMPT_VERSION = "guardrail-blocked"

_BLOCK_REASONS = {
    "threat": "The complaint contains threatening language. It was not sent to the agents; "
              "an analyst must handle it (and involve security if needed).",
    "self_harm": "The complaint mentions self-harm. It was not sent to the agents; an analyst "
                 "must contact the customer as a priority (customer welfare).",
}


@dataclass
class GuardedInput:
    request: DisputeRequest  # with the sanitised complaint_text
    report: dict[str, Any] = field(default_factory=dict)
    block_reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.block_reason is not None

    @property
    def flagged(self) -> bool:
        return bool(self.report["injection"]["flagged"]
                    or self.report["input_toxicity"]["flagged"])


def guard_input(request: DisputeRequest) -> GuardedInput:
    injection = detect_injection(request.complaint_text)
    toxicity = check_toxicity(injection.sanitized_text)
    report: dict[str, Any] = {
        "injection": {"flagged": injection.flagged, "rules": injection.rules,
                      "removed": injection.removed},
        "input_toxicity": {"flagged": toxicity.flagged, "categories": toxicity.categories},
    }
    text = toxicity.masked_text
    if text != request.complaint_text:
        # model_copy does not re-validate: the masked text may be under the 10-character
        # minimum if the whole complaint was an injection, which is fine for the agents.
        request = request.model_copy(update={"complaint_text": text})
    block_reason = None
    if toxicity.blocking:
        block_reason = " ".join(_BLOCK_REASONS[c] for c in toxicity.blocking)
    if injection.flagged or toxicity.flagged:
        log.warning("guardrail_input_flagged", injection_rules=injection.rules,
                    toxicity=toxicity.categories, blocked=block_reason is not None)
    return GuardedInput(request=request, report=report, block_reason=block_reason)


def blocked_result(guarded: GuardedInput, run_id: str) -> RunResult:
    """The escalation returned instead of running the agents."""
    trace = [{"step": GUARDRAIL_STEP, **guarded.report, "blocked": True,
              "output": {"violations": []}}]
    return RunResult(run_id=run_id, status=RunStatus.escalated, route=Route.escalate,
                     liability_position="undetermined",
                     basis="Stopped by the input guardrail before any agent ran.",
                     escalation_reason=guarded.block_reason,
                     prompt_version=BLOCKED_PROMPT_VERSION, trace=trace)


def guard_output(result: RunResult, guarded: GuardedInput) -> RunResult:
    violations, redacted = check_output(result)
    entry = {"step": GUARDRAIL_STEP, **guarded.report, "blocked": False,
             "output": {"violations": [{"rule": v.rule, "field": v.field, "text": v.text}
                                       for v in violations]}}
    update: dict[str, Any] = dict(redacted)
    if violations or guarded.flagged:
        # Only when a guardrail fired, so a clean run's trace is exactly the agent's.
        update["trace"] = [*result.trace, entry]
    if violations and result.status == RunStatus.completed:
        rules = sorted({v.rule for v in violations})
        update.update(
            status=RunStatus.escalated, route=Route.escalate,
            escalation_reason=("Output guardrail: the draft recommendation made a "
                               f"determination the service may not make ({', '.join(rules)}). "
                               f"Drafted route: {result.route}. An analyst must review it."),
        )
        entry["output"]["escalated"] = True
    if violations:
        log.warning("guardrail_output_violation", rules=[v.rule for v in violations],
                    status_before=str(result.status))
    # Re-validate so the RunResult contract (e.g. escalated => reason) still holds.
    return RunResult.model_validate({**result.model_dump(), **update})
