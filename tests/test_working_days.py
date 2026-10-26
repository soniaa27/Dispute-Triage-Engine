from datetime import date, datetime

import pytest

from contracts.tools import compute_working_days

STATE = "Karnataka"


def test_same_day_is_zero():
    assert compute_working_days(date(2026, 10, 7), date(2026, 10, 7), STATE) == 0


def test_friday_to_monday_with_working_first_saturday():
    # Fri 2 Oct 2026 -> Mon 5 Oct: Sat 3 (1st Saturday, working) + Mon 5; Sun 4 excluded.
    assert compute_working_days(date(2026, 10, 2), date(2026, 10, 5), STATE) == 2


def test_friday_to_monday_across_second_saturday():
    # Fri 9 Oct 2026 -> Mon 12 Oct: Sat 10 is the 2nd Saturday, Sun 11 is Sunday.
    assert compute_working_days(date(2026, 10, 9), date(2026, 10, 12), STATE) == 1


def test_fourth_saturday_is_non_working():
    # Fri 23 Oct 2026 -> Sat 24 Oct (4th Saturday).
    assert compute_working_days(date(2026, 10, 23), date(2026, 10, 24), STATE) == 0


def test_three_week_gap_exceeds_seven_working_days():
    # Section 4.1 scenario dates: alert 1 Jul 2026, reported 22 Jul 2026.
    # 21 calendar days minus Sundays 5/12/19 and 2nd Saturday 11.
    days = compute_working_days(date(2026, 7, 1), date(2026, 7, 22), STATE)
    assert days == 17
    assert days > 7


def test_holidays_are_excluded():
    holiday = date(2026, 10, 6)
    assert compute_working_days(date(2026, 10, 5), date(2026, 10, 7), STATE, {holiday}) == 1


def test_end_before_start_rejected():
    with pytest.raises(ValueError):
        compute_working_days(date(2026, 10, 7), date(2026, 10, 6), STATE)


def test_datetime_rejected():
    with pytest.raises(TypeError):
        compute_working_days(datetime(2026, 10, 5, 9), date(2026, 10, 7), STATE)
