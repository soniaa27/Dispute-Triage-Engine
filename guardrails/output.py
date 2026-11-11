"""Output filter: the determinations the service is not allowed to make.

The service recommends one of four routes to a disputes analyst. It never issues a credit,
never closes a dispute and never decides that a customer committed fraud (README, "Decision
support only"). The model writes free text into liability_position, basis and
escalation_reason, and nothing in the agents stops that text from saying "the amount has
been credited" or "the customer committed first-party fraud" (the demand in scenario S23).
This filter does, in code, on every result:

- action_taken: says a credit, refund or reversal was made, or that the dispute was closed.
- binding_decision: presents the recommendation as final or not open to review.
- fraud_accusation: says the customer committed or is suspected of (first-party) fraud.
- route_not_allowed: a completed run whose route is not one of ALLOWED_COMPLETED_ROUTES.
- toxic_output: any toxicity category in the text (guardrails/toxicity.py).

On a violation the offending sentences are replaced with REDACTED_MARKER and a completed
run is turned into an escalation: an analyst reads it, and the citations stay attached.
An escalated run stays escalated, with the text redacted.
"""

import re
from dataclasses import dataclass

from contracts import Route, RunStatus
from contracts.result import RunResult
from guardrails.toxicity import check_toxicity

REDACTED_MARKER = "[removed by output guardrail]"
TEXT_FIELDS = ("liability_position", "basis", "escalation_reason")
ALLOWED_COMPLETED_ROUTES = frozenset({Route.chargeback, Route.provisional_credit, Route.decline})

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("action_taken", re.compile(
        r"\b(?:has|have)\s+(?:already\s+)?been\s+(?:credited|refunded|reversed|reimbursed|"
        r"repaid|paid back|charged back)\b"
        r"|\b(?:we|the bank|i)\s+(?:have|has)\s+(?:now\s+)?(?:credited|refunded|reversed|"
        r"reimbursed|closed|resolved)\b"
        # Past or perfect tense only: the policies say a credit "is made" and a complaint "is
        # resolved" within N days (RBI 10(i), IDBI 7.2), which a correct answer may repeat.
        r"|\b(?:credit|refund|reversal|chargeback|reimbursement)\s+(?:has been|was|is now)\s+"
        r"(?:issued|processed|made|completed|initiated|granted|approved)\b"
        r"|\b(?:dispute|case|complaint)\s+(?:has been|was|is now)\s+(?:closed|resolved|"
        r"settled)\b"
        r"|\bclos(?:e|ing)\s+(?:the|this)\s+(?:dispute|case|complaint)\b", re.IGNORECASE)),
    ("binding_decision", re.compile(
        # Not a bare "final decision": "the analyst makes the final decision" is the disclaimer.
        r"\b(?:this|it)\s+is\s+(?:a|the|our)\s+final\s+(?:decision|determination|ruling)\b"
        r"|\b(?:this|the|our)\s+(?:decision|determination|recommendation)\s+is\s+"
        r"(?:final|binding)\b"
        r"|\bnot\s+(?:subject to|open to)\s+(?:review|appeal)\b"
        r"|\bno\s+further\s+(?:review|appeal)\s+(?:is\s+)?(?:required|needed|possible)\b",
        re.IGNORECASE)),
    ("fraud_accusation", re.compile(
        r"\bfirst[- ]party\s+fraud\b"
        r"|\b(?:customer|cardholder|complainant)\s+(?:is|was|has been)\s+(?:a\s+)?"
        r"(?:lying|fraudster|committing fraud|complicit|flagged for fraud)\b"
        r"|\b(?:customer|cardholder|complainant)\s+(?:committed|perpetrated|staged)\s+"
        r"(?:the\s+)?(?:fraud|transaction)\b", re.IGNORECASE)),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class OutputViolation:
    rule: str
    field: str
    text: str  # the offending sentence, or the route


def _sentence_rule(sentence: str) -> str | None:
    for name, pattern in _RULES:
        if pattern.search(sentence):
            return name
    if check_toxicity(sentence).flagged:
        return "toxic_output"
    return None


def check_output(result: RunResult) -> tuple[list[OutputViolation], dict[str, str]]:
    """(violations, {field: redacted text}) for every text field that needs a change."""
    violations: list[OutputViolation] = []
    redacted: dict[str, str] = {}
    for name in TEXT_FIELDS:
        value = getattr(result, name)
        if not value:
            continue
        kept, changed = [], False
        for sentence in _SENTENCE_SPLIT.split(value):
            rule = _sentence_rule(sentence)
            if rule is None:
                kept.append(sentence)
                continue
            violations.append(OutputViolation(rule, name, sentence.strip()))
            changed = True
            if not kept or kept[-1] != REDACTED_MARKER:
                kept.append(REDACTED_MARKER)
        if changed:
            redacted[name] = " ".join(kept)
    if result.status == RunStatus.completed and result.route not in ALLOWED_COMPLETED_ROUTES:
        violations.append(OutputViolation("route_not_allowed", "route", str(result.route)))
    return violations, redacted
