"""Agent entry point called by the worker.

TODO(Person A): replace this FAKE with the real multi-agent LangGraph/CrewAI run
(retrieve -> look for modifying provisions -> re-search -> verify -> retry/escalate).
Keep the signature and the RunResult contract unchanged so the worker and API need no edits.
"""

import time

from contracts import Channel, Citation, DisputeRequest, FraudType, Route, RunResult, RunStatus

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


def run_agent(req: DisputeRequest, run_id: str) -> RunResult:
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
