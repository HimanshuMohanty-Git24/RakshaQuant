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
    Return True if ``now`` falls within NSE trading hours on a weekday (IST).

    Args:
        now: Optional timezone-aware datetime. Defaults to the current IST time.
            A naive datetime is assumed to already be in IST.
    """
    if now is None:
        now = now_ist()
    # Market closed on weekends (Saturday=5, Sunday=6).
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE
