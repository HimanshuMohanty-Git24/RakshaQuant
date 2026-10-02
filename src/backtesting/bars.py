"""
Daily bars as intraday quotes (plan M11.1), so the paper engine itself can backtest.

A bar becomes a deterministic path the simulated broker and the exit manager can act on:

    09:15 open → 09:21 open → … open to low … → … low to high … → … high to close … → 15:25 close

* **Next-open fills:** decisions happen at 09:20 (the entry window); a market order fills at the
  next quote, the 09:21 one, at the open price (plus the fill model's spread and impact).
* **Adverse first:** within the bar the low comes before the high - the conservative order for
  long positions (a stop inside the bar is hit before a target).
* **Stops near their price, gaps at the open:** the legs are interpolated in ``legs`` steps, so a
  resting stop triggers on the first quote through it (within one step of the stop), and a bar
  that *opens* through a stop fills at the open - a gap fill.
* Volume accrues linearly to the bar's volume, for the fill model's participation cap.

This is an approximation of the day, documented as such: the true intraday path is unknown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from src.domain.calendar import NSECalendar
from src.domain.types import Bar, MarketDataSource, Quote
from src.marketdata.history import HistoryResult
from src.utils.market_time import IST

OPEN = time(9, 15)
FIRST_FILL = time(9, 21)  # the first quote after the 09:20 decision: next-open fills
LAST = time(15, 25)
LEGS = 8  # quotes per leg (open→low, low→high, high→close)


def _prices(bar: Bar, legs: int) -> list[float]:
    path = [bar.open, bar.open]
    for start, end in ((bar.open, bar.low), (bar.low, bar.high), (bar.high, bar.close)):
        path += [start + (end - start) * (i + 1) / legs for i in range(legs)]
    return path


def bar_path(
    bar: Bar,
    *,
    prev_close: float | None,
    legs: int = LEGS,
    source: MarketDataSource = MarketDataSource.REPLAY,
) -> list[Quote]:
    """The quotes one daily bar implies for its session (see the module docstring)."""
    day = bar.session_date
    first = datetime.combine(day, FIRST_FILL, IST)
    last = datetime.combine(day, LAST, IST)
    prices = _prices(bar, legs)
    steps = len(prices) - 2  # after the 09:15 and 09:21 opens
    times = [datetime.combine(day, OPEN, IST), first]
    times += [first + (last - first) * (i + 1) / steps for i in range(steps)]
    quotes = []
    for i, (ts, price) in enumerate(zip(times, prices, strict=True)):
        stamp = ts.replace(microsecond=0)
        quotes.append(Quote(
            instrument_key=bar.instrument_key, ltp=round(price, 2),
            prev_close=prev_close, volume_cum=int(bar.volume * (i + 1) / len(prices)),
            exchange_ts=stamp, receipt_ts=stamp, source=source,
        ))  # fmt: skip
    return quotes


def day_quotes(
    bars: Iterable[Bar], prev_closes: Mapping[str, float], *, legs: int = LEGS
) -> list[Quote]:
    """Every instrument's path for one session, in time order."""
    quotes = [q for b in bars for q in bar_path(b, prev_close=prev_closes.get(b.instrument_key),
                                                legs=legs)]  # fmt: skip
    return sorted(quotes, key=lambda q: (q.receipt_ts, q.instrument_key))


def sessions(bars: Sequence[Bar]) -> dict[date, list[Bar]]:
    """Raw (unadjusted) bars by session date - what traded that day."""
    out: dict[date, list[Bar]] = {}
    for b in bars:
        if not b.adjusted:
            out.setdefault(b.session_date, []).append(b)
    return out


def previous_closes(bars: Sequence[Bar], day: date) -> dict[str, float]:
    """Each instrument's last raw close before ``day``."""
    latest: dict[str, Bar] = {}
    for b in bars:
        if not b.adjusted and b.session_date < day:
            seen = latest.get(b.instrument_key)
            if seen is None or b.session_date > seen.session_date:
                latest[b.instrument_key] = b
    return {k: b.close for k, b in latest.items()}


def calendar_from_bars(bars: Iterable[Bar]) -> NSECalendar:
    """A calendar whose sessions are the days the data traded (standard hours), for backtests
    over years the NSE calendar file does not cover: every other weekday is a holiday."""
    days = {b.session_date for b in bars}
    years = sorted({d.year for d in days})
    spec: dict[str, Any] = {}
    for year in years:
        weekday = date(year, 1, 1)
        holidays = []
        while weekday.year == year:
            if weekday.weekday() < 5 and weekday not in days:
                holidays.append({"date": weekday.isoformat(), "name": "no session in the data"})
            weekday += timedelta(days=1)
        spec[str(year)] = {"holidays": holidays}
    session = {"pre_open": "09:00", "open": "09:15", "close": "15:30"}
    return NSECalendar({"schema_version": 1, "default_session": session, "years": spec})


def bars_from_history(result: HistoryResult) -> list[Bar]:
    """Fetched history (``HistoryResult``) as the bars a backtest replays: raw and adjusted."""
    return [bar for series in result.series.values()
            for adjusted in (False, True) for bar in series.bars(adjusted=adjusted)]  # fmt: skip
