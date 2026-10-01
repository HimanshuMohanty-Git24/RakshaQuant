"""
Quote validation (plan M2.2; audit §K.4). A quote is dropped, with a ``QuoteRejected`` event,
when:

* its price is not finite or not positive;
* it moved more than ``band + 2%`` from the previous close (a bad tick, not a real move);
* its cumulative day volume went *down* versus the last accepted quote of the same session.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime

from pydantic import JsonValue

from src.domain.types import MarketDataSource, Quote
from src.utils.market_time import IST

# Default circuit band when an instrument has none: NIFTY 50 names are F&O stocks without a
# fixed band (dynamic bands start at 10% and can flex), so validation uses a generous 20%.
DEFAULT_BAND_PCT = 20.0
BAND_TOLERANCE_PCT = 2.0


@dataclass(frozen=True, slots=True)
class RawQuote:
    """A vendor quote before validation (floats may be NaN here; ``Quote`` forbids that)."""

    instrument_key: str
    ltp: float
    volume_cum: int
    exchange_ts: datetime
    prev_close: float | None = None

    def with_prev_close(self, prev_close: float | None) -> RawQuote:
        return replace(self, prev_close=prev_close)

    def as_json(self) -> dict[str, JsonValue]:
        def num(x: float | None) -> JsonValue:
            return x if x is None or math.isfinite(x) else str(x)

        return {
            "ltp": num(self.ltp),
            "prev_close": num(self.prev_close),
            "volume_cum": self.volume_cum,
            "exchange_ts": self.exchange_ts.isoformat(),
        }

    def to_quote(self, *, receipt_ts: datetime, source: MarketDataSource) -> Quote:
        return Quote(
            instrument_key=self.instrument_key,
            ltp=self.ltp,
            prev_close=self.prev_close if self.prev_close and self.prev_close > 0 else None,
            volume_cum=self.volume_cum,
            exchange_ts=self.exchange_ts,
            receipt_ts=receipt_ts,
            source=source,
            is_delayed=source is MarketDataSource.YFINANCE,
        )


@dataclass(frozen=True, slots=True)
class _Seen:
    session_date: date
    volume_cum: int


class QuoteValidator:
    def __init__(
        self,
        band_pct_for: Callable[[str], float] | None = None,
        *,
        tolerance_pct: float = BAND_TOLERANCE_PCT,
    ) -> None:
        self._band_pct_for = band_pct_for or (lambda _key: DEFAULT_BAND_PCT)
        self._tolerance_pct = tolerance_pct
        self._seen: dict[str, _Seen] = {}

    def check(self, quote: RawQuote) -> str | None:
        """Return a rejection reason, or None if the quote is acceptable."""
        if not math.isfinite(quote.ltp):
            return "non_finite_price"
        if quote.ltp <= 0:
            return "non_positive_price"
        if quote.volume_cum < 0:
            return "negative_volume"
        if quote.exchange_ts.tzinfo is None:
            return "naive_timestamp"
        prev = quote.prev_close
        if prev is not None and math.isfinite(prev) and prev > 0:
            move_pct = abs(quote.ltp / prev - 1.0) * 100.0
            if move_pct > self._band_pct_for(quote.instrument_key) + self._tolerance_pct:
                return "out_of_band"
        seen = self._seen.get(quote.instrument_key)
        if (
            seen is not None
            and seen.session_date == _session_date(quote.exchange_ts)
            and quote.volume_cum < seen.volume_cum
        ):
            return "volume_decreased"
        return None

    def accept(self, quote: Quote) -> None:
        """Remember an accepted quote (its session's cumulative volume)."""
        self._seen[quote.instrument_key] = _Seen(_session_date(quote.exchange_ts), quote.volume_cum)


def _session_date(ts: datetime) -> date:
    return ts.astimezone(IST).date()
