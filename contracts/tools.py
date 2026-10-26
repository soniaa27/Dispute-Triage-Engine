"""Tools shared by both agents: the Retriever interface and working-day arithmetic."""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

from contracts.agent import DocId, Passage

SUNDAY = 6
SATURDAY = 5
NON_WORKING_SATURDAYS = frozenset({2, 4})  # 2nd and 4th Saturday of the month


@runtime_checkable
class Retriever(Protocol):
    """Corpus access. Implemented by Person 1 over the FAISS/Chroma index."""

    def search_corpus(
        self, query: str, k: int = 5, doc_ids: Sequence[DocId] | None = None
    ) -> list[Passage]:
        """Top-k passages for a query, best first, optionally restricted to some documents."""
        ...

    def get_clause(self, doc_id: DocId, clause_id: str) -> Passage | None:
        """Exact lookup by clause_id (see docs/interfaces.md); None if it does not exist."""
        ...


def _is_working_day(day: date, holidays: set[date]) -> bool:
    if day.weekday() == SUNDAY or day in holidays:
        return False
    if day.weekday() == SATURDAY:
        nth_saturday = (day.day - 1) // 7 + 1
        return nth_saturday not in NON_WORKING_SATURDAYS
    return True


def compute_working_days(
    start: date, end: date, home_state: str, holidays: set[date] | None = None
) -> int:
    """Working days after `start` up to and including `end`; `start` itself is not counted.

    Used for the reporting delay: `start` is the date the customer received the bank's
    communication, `end` the date of reporting, both as calendar dates at the home branch.

    Corpus basis:
    - IDBI_POLICY 5.13 "Number of Days": "is calculated without considering the date of
      reporting and includes only working days in the Home Branch of the Client."
    - RBI_2017_CIRCULAR, note under Table 2: working days are "counted as per the working
      schedule of the home branch of the customer excluding the date of receiving the
      communication."
    The two differ on which end date is excluded. This function follows the RBI note
    (exclude the communication date, count the reporting date); see docs/decisions.md.

    Non-working days: Sundays, the 2nd and 4th Saturdays of the month, and `holidays`.

    TODO: load state bank-holiday calendars keyed by `home_state` (Negotiable
    Instruments Act holidays per state); until then pass them via `holidays`.
    """
    for name, value in (("start", start), ("end", end)):
        if isinstance(value, datetime):
            raise TypeError(f"{name} must be a date in the home branch's local time, not datetime")
    if end < start:
        raise ValueError("end must not be before start")
    holidays = holidays or set()
    days = (end - start).days
    return sum(
        _is_working_day(start + timedelta(days=offset), holidays)
        for offset in range(1, days + 1)
    )
