"""
NSE trading calendar (plan M1.4).

Loaded from ``src/config/nse_calendar.json`` (holidays and special sessions per year, each with
its NSE source). Rules:

* A weekday is a trading day unless it is a holiday. A weekend day is a trading day only if a
  special session with notified times falls on it (e.g. Budget day, Muhurat).
* A special session whose times are not yet notified leaves the date **closed**.
* Dates in years the file does not cover raise :class:`CalendarCoverageError`: the calendar
  never guesses (fail closed). Add the next year as soon as NSE publishes it.
* The normal market is the half-open interval ``[open, close)`` in IST.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.utils.market_time import IST

DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "config" / "nse_calendar.json"

# Searching for the next/previous trading day never needs more than this many days.
_MAX_SCAN_DAYS = 31


class CalendarCoverageError(LookupError):
    """The date is outside the years the calendar file covers."""


@dataclass(frozen=True, slots=True)
class SessionTimes:
    pre_open: time
    open: time
    close: time

    def __post_init__(self) -> None:
        if not self.pre_open <= self.open < self.close:
            raise ValueError(f"session times out of order: {self}")


@dataclass(frozen=True, slots=True)
class Session:
    """One trading session; datetimes are timezone-aware IST."""

    day: date
    pre_open: datetime
    open: datetime
    close: datetime
    special: str | None = None  # name of the special session, if any

    def contains(self, ts: datetime) -> bool:
        return self.open <= ts < self.close


@dataclass(frozen=True, slots=True)
class _SpecialSession:
    name: str
    times: SessionTimes | None  # None: announced, timings not yet notified


class NSECalendar:
    def __init__(self, data: Mapping[str, Any]) -> None:
        if data.get("schema_version") != 1:
            raise ValueError(f"unsupported calendar schema {data.get('schema_version')!r}")
        self._default = _parse_times(data["default_session"])
        self._years: frozenset[int] = frozenset(int(y) for y in data["years"])
        self._holidays: dict[date, str] = {}
        self._specials: dict[date, _SpecialSession] = {}
        for year, spec in data["years"].items():
            for item in spec.get("holidays", []):
                day = _parse_day(item["date"], int(year))
                self._holidays[day] = str(item["name"])
            for item in spec.get("special_sessions", []):
                day = _parse_day(item["date"], int(year))
                times = None if item.get("open") is None else _parse_times(item)
                self._specials[day] = _SpecialSession(str(item["name"]), times)

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CALENDAR_PATH) -> NSECalendar:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    # -- coverage ------------------------------------------------------------------------

    @property
    def covered_years(self) -> frozenset[int]:
        return self._years

    def covers(self, day: date) -> bool:
        return day.year in self._years

    def _require(self, day: date) -> None:
        if not self.covers(day):
            raise CalendarCoverageError(
                f"{day} is outside the NSE calendar (covered years: {sorted(self._years)}); "
                "update src/config/nse_calendar.json from the NSE holiday circular"
            )

    # -- days ------------------------------------------------------------------------------

    def holiday_name(self, day: date) -> str | None:
        self._require(day)
        return self._holidays.get(day)

    def session(self, day: date) -> Session | None:
        """The session on ``day``, or None if the market does not trade that day."""
        self._require(day)
        special = self._specials.get(day)
        if special is not None:
            if special.times is None:
                return None
            return _session(day, special.times, special.name)
        if day.weekday() >= 5 or day in self._holidays:
            return None
        return _session(day, self._default, None)

    def is_trading_day(self, day: date) -> bool:
        return self.session(day) is not None

    def next_trading_day(self, day: date) -> date:
        """The first trading day strictly after ``day``."""
        return self._scan(day, 1)

    def previous_trading_day(self, day: date) -> date:
        """The last trading day strictly before ``day``."""
        return self._scan(day, -1)

    def trading_days(self, start: date, end: date) -> list[date]:
        """Trading days in ``[start, end]``."""
        days: list[date] = []
        day = start
        while day <= end:
            if self.is_trading_day(day):
                days.append(day)
            day += timedelta(days=1)
        return days

    def _scan(self, day: date, step: int) -> date:
        candidate = day
        for _ in range(_MAX_SCAN_DAYS):
            candidate += timedelta(days=step)
            if self.is_trading_day(candidate):
                return candidate
        raise CalendarCoverageError(f"no trading day within {_MAX_SCAN_DAYS} days of {day}")

    # -- instants ----------------------------------------------------------------------------

    def is_market_open(self, ts: datetime) -> bool:
        """True during the normal market of a session (``[open, close)`` IST)."""
        local = _as_ist(ts)
        session = self.session(local.date())
        return session is not None and session.contains(local)

    def next_session(self, ts: datetime) -> Session:
        """The session in progress at ``ts``, else the next one to open."""
        local = _as_ist(ts)
        today = self.session(local.date())
        if today is not None and local < today.close:
            return today
        nxt = self.session(self.next_trading_day(local.date()))
        if nxt is None:  # pragma: no cover - next_trading_day only returns trading days
            raise RuntimeError("next_trading_day returned a non-trading day")
        return nxt


def _as_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("calendar instants must be timezone-aware")
    return ts.astimezone(IST)


def _session(day: date, times: SessionTimes, special: str | None) -> Session:
    return Session(
        day=day,
        pre_open=datetime.combine(day, times.pre_open, IST),
        open=datetime.combine(day, times.open, IST),
        close=datetime.combine(day, times.close, IST),
        special=special,
    )


def _parse_times(spec: Mapping[str, Any]) -> SessionTimes:
    return SessionTimes(
        pre_open=time.fromisoformat(spec["pre_open"]),
        open=time.fromisoformat(spec["open"]),
        close=time.fromisoformat(spec["close"]),
    )


def _parse_day(text: str, year: int) -> date:
    day = date.fromisoformat(text)
    if day.year != year:
        raise ValueError(f"{text} is listed under year {year}")
    return day


@lru_cache(maxsize=1)
def get_calendar() -> NSECalendar:
    """The process-wide calendar loaded from ``src/config/nse_calendar.json``."""
    return NSECalendar.from_file()
