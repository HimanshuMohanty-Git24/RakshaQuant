"""
Replaying a recorded tape (plan M5.6 acceptance; audit §G.4): the same engine, driven by a
:class:`~src.domain.clock.ReplayClock` over what the system received on a past day.

* :class:`TapeQuoteSource` serves, on each poll, every instrument's latest taped quote received
  at or before the clock - exactly what a live poll would have returned at that moment.
* :class:`TapeHistorySource` rebuilds the day's daily history from the taped bars.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime

from src.domain.clock import Clock
from src.domain.types import Bar, Instrument, Quote
from src.marketdata.history import DEFAULT_MIN_BARS, HistoryResult, series_from_bars
from src.marketdata.yfinance_source import PollResult


class TapeQuoteSource:
    def __init__(
        self, quotes: Iterable[Quote], *, clock: Clock, poll_interval_s: float = 60.0
    ) -> None:
        by_key: dict[str, list[Quote]] = defaultdict(list)
        for quote in quotes:
            by_key[quote.instrument_key].append(quote)
        self._quotes = {k: sorted(v, key=lambda q: q.receipt_ts) for k, v in by_key.items()}
        self._times = {k: [q.receipt_ts for q in v] for k, v in self._quotes.items()}
        self._clock = clock
        self._interval = poll_interval_s
        self.last_quotes: dict[str, Quote] = {}

    @property
    def next_delay_s(self) -> float:
        return self._interval

    @property
    def stale_keys(self) -> frozenset[str]:
        return frozenset()

    def set_prev_closes(self, closes: Mapping[str, float]) -> None:
        """Taped quotes already carry their previous close."""

    def latest(self, instrument_key: str, at: datetime) -> Quote | None:
        times = self._times.get(instrument_key)
        if not times:
            return None
        i = bisect.bisect_right(times, at)
        return self._quotes[instrument_key][i - 1] if i else None

    async def poll(self) -> PollResult:
        now = self._clock.now()
        fresh: dict[str, Quote] = {}
        for key in self._quotes:
            quote = self.latest(key, now)
            if quote is not None and self.last_quotes.get(key) != quote:
                fresh[key] = quote
                self.last_quotes[key] = quote
        return PollResult(ok=True, quotes=fresh)


class TapeHistorySource:
    def __init__(self, bars: Sequence[Bar], *, min_bars: int = DEFAULT_MIN_BARS) -> None:
        self._bars = list(bars)
        self._min_bars = min_bars

    async def fetch(
        self,
        instruments: Sequence[Instrument],
        *,
        settled_before: date,
        expected_last: date | None,
        extra: Mapping[str, str] | None = None,
    ) -> HistoryResult:
        keys = {i.key for i in instruments} | set(extra or {})
        result = series_from_bars(
            (b for b in self._bars if b.instrument_key in keys),
            settled_before=settled_before,
            min_bars=self._min_bars,
            expected_last=expected_last,
        )
        failed = dict(result.failed)
        for key in sorted(keys - result.series.keys() - failed.keys()):
            failed[key] = "not on the tape"
        return HistoryResult(series=result.series, failed=failed, lagging=result.lagging)
