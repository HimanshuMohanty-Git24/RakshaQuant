"""
Market time helpers.

NSE operates in India Standard Time (IST). IST is a fixed UTC+05:30 offset with
no daylight-saving transitions, so a fixed ``timezone`` is exactly correct and
avoids depending on the system tz database (which is absent on Windows).

Using these helpers everywhere market-hour decisions are made guarantees the same
behaviour regardless of the host's local timezone (e.g. a UTC cloud server or CI
runner) — previously ``datetime.now()`` was compared against IST constants, which
was wrong by the host's UTC offset.
"""

from datetime import datetime, time, timedelta, timezone

# India Standard Time: fixed UTC+05:30, no DST.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# NSE equity market hours (IST).
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    """Return the current timezone-aware datetime in IST."""
    return datetime.now(IST)


def is_market_hours(now: datetime | None = None) -> bool:
    """
    Return True if ``now`` falls within an NSE normal-market session (IST).

    Decided by the NSE calendar (``src/domain/calendar.py``): holidays, weekends and special
    sessions included. Raises ``CalendarCoverageError`` for years the calendar does not cover.

    Args:
        now: Optional timezone-aware datetime. Defaults to the current IST time.
            A naive datetime is assumed to already be in IST.
    """
    from src.domain.calendar import get_calendar  # local: the calendar imports IST from here

    if now is None:
        now = now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    return get_calendar().is_market_open(now)
