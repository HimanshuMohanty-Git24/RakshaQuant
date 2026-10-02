"""Plan M1.4: the NSE calendar (holidays, special sessions, coverage) and its consumers."""

import copy
import json
from datetime import UTC, date, datetime, time

import pytest

from src.domain.calendar import (
    DEFAULT_CALENDAR_PATH,
    CalendarCoverageError,
    NSECalendar,
    get_calendar,
)
from src.utils.market_time import IST, is_market_hours

CAL = get_calendar()
RAW = json.loads(DEFAULT_CALENDAR_PATH.read_text(encoding="utf-8"))


def ist(*args: int) -> datetime:
    return datetime(*args, tzinfo=IST)


def _with_muhurat_times() -> NSECalendar:
    """The real 2026 calendar with Muhurat times filled in (illustrative; NSE has not yet
    notified the 2026 timings, so these follow the 2025 precedent of 13:45-14:45)."""
    data = copy.deepcopy(RAW)
    for item in data["years"]["2026"]["special_sessions"]:
        if item["date"] == "2026-11-08":
            item.update(pre_open="13:30", open="13:45", close="14:45")
    return NSECalendar(data)


# --- acceptance cases ---------------------------------------------------------------------


def test_weekday_is_a_normal_session():
    s = CAL.session(date(2026, 10, 5))  # Monday
    assert s is not None and s.special is None
    assert (s.pre_open, s.open, s.close) == (
        ist(2026, 10, 5, 9, 0),
        ist(2026, 10, 5, 9, 15),
        ist(2026, 10, 5, 15, 30),
    )


def test_weekend_is_closed():
    assert not CAL.is_trading_day(date(2026, 10, 3))  # Saturday
    assert not CAL.is_trading_day(date(2026, 10, 4))  # Sunday


def test_gandhi_jayanti_2026_is_a_holiday():
    assert not CAL.is_trading_day(date(2026, 10, 2))
    assert CAL.holiday_name(date(2026, 10, 2)) == "Mahatma Gandhi Jayanti"
    assert not CAL.is_market_open(ist(2026, 10, 2, 11, 0))


def test_muhurat_is_closed_until_nse_notifies_the_timings():
    day = date(2026, 11, 8)  # Sunday, Diwali Laxmi Pujan
    assert CAL.session(day) is None
    assert not CAL.is_market_open(ist(2026, 11, 8, 18, 30))


def test_muhurat_session_trades_once_timed():
    cal = _with_muhurat_times()
    s = cal.session(date(2026, 11, 8))
    assert s is not None and s.special == "Muhurat trading (Diwali Laxmi Pujan)"
    assert cal.is_market_open(ist(2026, 11, 8, 14, 0))
    assert not cal.is_market_open(ist(2026, 11, 8, 10, 0))  # normal hours don't apply
    assert not cal.is_market_open(ist(2026, 11, 8, 14, 45))
    assert cal.holiday_name(date(2026, 11, 8)) == "Diwali Laxmi Pujan"


# --- verified 2026 facts ------------------------------------------------------------------


def test_sunday_budget_session_2026():
    s = CAL.session(date(2026, 2, 1))
    assert s is not None and s.special == "Union Budget live trading session"
    assert CAL.is_market_open(ist(2026, 2, 1, 11, 0))


def test_adhoc_election_holiday_on_a_thursday():
    assert CAL.holiday_name(date(2026, 1, 15)) == "Municipal Corporation Election - Maharashtra"
    assert not CAL.is_trading_day(date(2026, 1, 15))


def test_october_2026_has_20_sessions():
    days = CAL.trading_days(date(2026, 10, 1), date(2026, 10, 31))
    assert len(days) == 20  # 22 weekdays minus Gandhi Jayanti and Dussehra
    assert date(2026, 10, 2) not in days and date(2026, 10, 20) not in days


def test_calendar_file_lists_every_2026_holiday_with_sources():
    year = RAW["years"]["2026"]
    assert len(year["holidays"]) == 20
    assert year["sources"]
    assert CAL.covered_years == frozenset({2026})


# --- instants and boundaries ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("clock", "open_"),
    [
        (time(9, 14, 59), False),
        (time(9, 15), True),
        (time(15, 29, 59), True),
        (time(15, 30), False),
    ],
)
def test_normal_market_is_half_open(clock, open_):
    assert CAL.is_market_open(datetime.combine(date(2026, 10, 5), clock, IST)) is open_


def test_instants_are_converted_to_ist():
    assert CAL.is_market_open(datetime(2026, 10, 5, 4, 30, tzinfo=UTC))  # 10:00 IST
    assert not CAL.is_market_open(datetime(2026, 10, 5, 10, 30, tzinfo=UTC))  # 16:00 IST
    with pytest.raises(ValueError, match="timezone-aware"):
        CAL.is_market_open(datetime(2026, 10, 5, 10, 0))


def test_next_session():
    # A 09:00 start waits for today's session.
    assert CAL.next_session(ist(2026, 10, 5, 9, 0)).day == date(2026, 10, 5)
    # During a session: that session.
    assert CAL.next_session(ist(2026, 10, 5, 12, 0)).day == date(2026, 10, 5)
    # Thursday evening before Gandhi Jayanti: skips the holiday and the weekend.
    assert CAL.next_session(ist(2026, 10, 1, 20, 0)).open == ist(2026, 10, 5, 9, 15)


def test_trading_day_navigation():
    assert CAL.next_trading_day(date(2026, 10, 1)) == date(2026, 10, 5)
    assert CAL.previous_trading_day(date(2026, 10, 5)) == date(2026, 10, 1)
    assert CAL.previous_trading_day(date(2026, 10, 21)) == date(2026, 10, 19)  # over Dussehra


# --- fail closed ------------------------------------------------------------------------------


@pytest.mark.parametrize("day", [date(2025, 12, 31), date(2027, 1, 4)])
def test_uncovered_years_raise(day):
    assert not CAL.covers(day)
    with pytest.raises(CalendarCoverageError, match="nse_calendar.json"):
        CAL.is_trading_day(day)


def test_navigation_past_coverage_raises():
    with pytest.raises(CalendarCoverageError):
        CAL.next_trading_day(date(2026, 12, 31))


def test_bad_calendar_data_is_refused():
    bad_schema = {**RAW, "schema_version": 2}
    with pytest.raises(ValueError, match="schema"):
        NSECalendar(bad_schema)
    wrong_year = copy.deepcopy(RAW)
    wrong_year["years"]["2026"]["holidays"].append({"date": "2027-01-26", "name": "x"})
    with pytest.raises(ValueError, match="listed under year"):
        NSECalendar(wrong_year)
    bad_times = copy.deepcopy(RAW)
    bad_times["default_session"] = {"pre_open": "09:00", "open": "15:30", "close": "09:15"}
    with pytest.raises(ValueError, match="out of order"):
        NSECalendar(bad_times)


# --- consumers --------------------------------------------------------------------------------


def test_is_market_hours_uses_the_calendar():
    assert is_market_hours(ist(2026, 10, 5, 10, 0))
    assert not is_market_hours(ist(2026, 10, 2, 10, 0))  # holiday, a weekday
    assert is_market_hours(datetime(2026, 10, 5, 10, 0))  # naive = IST (legacy contract)
