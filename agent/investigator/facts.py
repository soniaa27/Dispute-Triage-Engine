"""Case facts the Investigator computes in code, before the model sees the case.

The reporting delay picks the liability-table row, so it is never left to the model
(docs/decisions.md D-009): it comes from contracts.tools.compute_working_days and is handed
to the model as a fact, together with a plain statement of how it was counted.
"""

from dataclasses import dataclass

from contracts.dispute import DisputeRequest
from contracts.tools import compute_working_days

# Delays at which the RBI and IDBI counting rules can disagree (D-009).
BOUNDARY_DELAYS = frozenset({3, 7})
# IDBI Table-I-note: reports beyond this many calendar days carry full customer liability.
CALENDAR_DAY_LIMIT = 60


@dataclass(frozen=True)
class CaseFacts:
    working_days_delay: int | None
    delay_basis: str
    calendar_days: int
    on_boundary: bool


def case_facts(request: DisputeRequest) -> CaseFacts:
    """Delay from the bank's alert to the customer's report, as calendar dates written in
    the request (+05:30, the home branch's local time)."""
    reported = request.reported_datetime.date()
    start = request.bank_alert_datetime or request.transaction_datetime
    calendar_days = (reported - start.date()).days
    if request.bank_alert_datetime is None:
        return CaseFacts(
            working_days_delay=None,
            delay_basis=(
                "No bank alert time was given, so the delay from the bank's communication "
                f"cannot be counted. The report came {calendar_days} calendar days after the "
                f"transaction ({start.date()} to {reported})."
            ),
            calendar_days=calendar_days,
            on_boundary=False,
        )

    alert = request.bank_alert_datetime.date()
    delay = compute_working_days(alert, reported, request.home_branch_state)
    on_boundary = delay in BOUNDARY_DELAYS
    basis = (
        f"{delay} working day{'' if delay == 1 else 's'} from the bank alert on {alert} to "
        f"the report on {reported} "
        f"({calendar_days} calendar days). Counted per the RBI circular's note under "
        "Table 2: the alert date is excluded, the reporting date included; Sundays and "
        "2nd/4th Saturdays are non-working. State bank holidays for "
        f"{request.home_branch_state} are not modelled."
    )
    if on_boundary:
        basis += (
            f" {delay} is a table boundary where the RBI and IDBI counting rules can differ "
            "(IDBI 5.13 excludes the reporting date instead)."
        )
    return CaseFacts(delay, basis, calendar_days, on_boundary)
