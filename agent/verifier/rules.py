"""Stage c: hard rules, in code. Any hit escalates, whatever a model would say, and no LLM
call is spent on the draft.

Rules, in priority order (the first hit sets the outcome; every hit is in the reason):

1. Out of scope (outcome out_of_scope):
   a. the draft cites no clause;
   b. the fraud type is "unknown" and no cited clause passed the citation check.
2. Boundary delay (outcome conflicting_provisions): the working-day delay, recomputed here
   with compute_working_days, is exactly 3 or exactly 7. RBI's note under Table 2 and IDBI
   5.13 count the days differently, and only at these values does that change the band
   (docs/decisions.md D-009; scenarios S17, S18).
3. Fault unclear (outcome contested_fault): draft.fault_hypothesis is "unclear".
4. The customer's own statements contradict the draft's fault (outcome contested_fault):
   a. customer_claims.shared_otp_or_pin is true, but the draft puts fault on the bank or a
      third party. Sharing an OTP or PIN is negligence in IDBI 5.12; whether it is also a
      third party breach (a caller posing as the bank) is the contested reading of S14.
   b. customer_claims.shared_otp_or_pin is false, but the draft says customer negligence.
      The customer denies the act negligence rests on: S16 (the bank's record says the OTP
      was entered, the customer denies it) and S15 (IDBI 5.12 reads the SIM swap as
      negligence, the customer shared nothing).
   A claim that is null ("not stated") never triggers a rule. Checked against every
   scenario: given the expected draft, rules fire on exactly the scenarios that must
   escalate (tests/test_verifier.py).

Considered and rejected (no document or scenario supports them):
- received_alert false -> escalate. S07 and S08 say the alert was not received and still
  expect decline: the delay runs from the bank's communication.
- card_in_possession false against a skimming or card-not-present draft. No scenario or
  provision ties possession to a fault reading.
- Keyword rules on the complaint ("SIM", "called saying he was from the bank"). Free text,
  not the customer's structured claims; left to the Investigator's prompt rule 7 and the
  Verifier's search for conflicting provisions.
- More than 60 calendar days -> escalate. IDBI Table-I-note settles it (S08, S24 decline).
- No bank alert time -> escalate. No scenario covers it; the Investigator states the delay
  as unknown and the check stage tests whatever band the draft relies on.
"""

from dataclasses import dataclass

from contracts.agent import FaultHypothesis, InvestigatorOutput, VerificationOutcome
from contracts.dispute import DisputeRequest
from contracts.result import FraudType
from contracts.tools import compute_working_days

# Delays at which the RBI and IDBI counting rules put the case in different bands (D-009).
BOUNDARY_DELAYS = frozenset({3, 7})

_NO_FAULT_BY_CUSTOMER = frozenset({FaultHypothesis.bank, FaultHypothesis.third_party})


@dataclass(frozen=True)
class RuleHit:
    rule: str
    outcome: VerificationOutcome
    reason: str


def recompute_delay(request: DisputeRequest) -> int | None:
    """Working days from the bank's alert to the report, as calendar dates in the request's
    own offset (the home branch's local time); None without an alert time. The Verifier
    recomputes it rather than trusting the draft (D-009)."""
    if request.bank_alert_datetime is None:
        return None
    return compute_working_days(request.bank_alert_datetime.date(),
                                request.reported_datetime.date(), request.home_branch_state)


def hard_rules(request: DisputeRequest, draft: InvestigatorOutput, delay: int | None,
               verified_citations: int) -> list[RuleHit]:
    """Every rule the draft breaks, highest priority first; empty if none."""
    hits = []
    if not draft.cited_clause_ids:
        hits.append(RuleHit("no_citation", VerificationOutcome.out_of_scope,
                            "the draft cites no provision, so the corpus does not cover "
                            "the case"))
    elif draft.fraud_type == FraudType.unknown and verified_citations == 0:
        hits.append(RuleHit("unknown_unsupported", VerificationOutcome.out_of_scope,
                            "the fraud type is unknown and no cited provision resolves to "
                            "retrieved text"))
    if delay in BOUNDARY_DELAYS:
        hits.append(RuleHit("boundary_delay", VerificationOutcome.conflicting_provisions,
                            f"the reporting delay is exactly {delay} working days, a table "
                            "boundary where the RBI note under Table 2 and IDBI 5.13 count "
                            "differently (D-009)"))
    if draft.fault_hypothesis == FaultHypothesis.unclear:
        hits.append(RuleHit("fault_unclear", VerificationOutcome.contested_fault,
                            "the Investigator could not attribute fault"))
    shared = request.customer_claims.shared_otp_or_pin
    if shared is True and draft.fault_hypothesis in _NO_FAULT_BY_CUSTOMER:
        hits.append(RuleHit("claims_shared_credentials", VerificationOutcome.contested_fault,
                            "the customer says they shared an OTP or PIN, which IDBI 5.12 "
                            f"lists as negligence, but the draft puts fault on "
                            f"{draft.fault_hypothesis}"))
    if shared is False and draft.fault_hypothesis == FaultHypothesis.customer_negligence:
        hits.append(RuleHit("claims_deny_sharing", VerificationOutcome.contested_fault,
                            "the customer says they shared no OTP or PIN, but the draft "
                            "finds customer negligence"))
    return hits
