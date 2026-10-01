"""
Batched, non-blocking YFinance quote poller (plan M2.1; audit §D, F.0).

One ``yf.download`` call per poll covers every symbol (1-minute bars for the current session),
made in a worker thread under a timeout so the event loop never blocks. Quotes are honest about
time: ``exchange_ts`` is the timestamp of the last 1-minute bar Yahoo returned (it lags the
market by ~15 minutes), ``receipt_ts`` is when we received it, and ``is_delayed`` is True.

Failure handling:

* A batched download swallows per-ticker errors (rate limits included) and returns NaN columns.
  If *no* symbol returns data, or the call raises or times out, the poll failed: the source
  backs off exponentially and emits ``FeedStale`` for the whole feed (``FeedRecovered`` later).
* Symbols that return nothing, or whose quote is older than ``stale_after_s`` while the market
  is open, are stale individually (``FeedStale`` / ``FeedRecovered`` on each transition).

Valid quotes are written to the Parquet tape.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.domain.clock import Clock
from src.domain.events import FeedRecovered, FeedStale, QuoteRejected
from src.domain.sink import EventSink
from src.domain.types import Instrument, MarketDataSource, Quote
from src.marketdata.symbols import yahoo_ticker
from src.marketdata.validation import QuoteValidator, RawQuote
from src.store.tape import TapeWriter

logger = logging.getLogger(__name__)

SOURCE = MarketDataSource.YFINANCE
# Audit §F.0: YFinance quotes are judged stale on exchange_ts after 1,200 s (it lags ~15 min).
DEFAULT_STALE_AFTER_S = 1200.0

Downloader = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class PollResult:
    ok: bool
    quotes: dict[str, Quote]  # valid quotes by instrument key
    missing: tuple[str, ...] = ()  # requested, but no data came back
    rejected: tuple[str, ...] = ()  # failed validation and were dropped
    error: str | None = None
    latency_s: float = 0.0


def _default_download(*args: Any, **kwargs: Any) -> Any:
    import yfinance as yf  # imported lazily: heavy, and tests inject a fake

    return yf.download(*args, **kwargs)


class YFinanceQuoteSource:
    def __init__(
        self,
        instruments: Sequence[Instrument],
        *,
        clock: Clock,
        sink: EventSink,
        tape: TapeWriter | None = None,
        validator: QuoteValidator | None = None,
        market_open: Callable[[datetime], bool] | None = None,
        download: Downloader | None = None,
        timeout_s: float = 20.0,
        poll_interval_s: float = 60.0,
        max_backoff_s: float = 900.0,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ) -> None:
        if not instruments:
            raise ValueError("the quote source needs at least one instrument")
        self._by_ticker = {yahoo_ticker(i.symbol): i.key for i in instruments}
        self._clock = clock
        self._sink = sink
        self._tape = tape
        self._validator = validator or QuoteValidator()
        self._market_open = market_open or (lambda _ts: True)
        self._download = download or _default_download
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        self._max_backoff_s = max_backoff_s
        self._stale_after_s = stale_after_s
        self._prev_close: dict[str, float] = {}
        self._failures = 0
        self._feed_down_since: datetime | None = None
        self._stale_since: dict[str, datetime] = {}
        self._inflight: asyncio.Future[dict[str, RawQuote]] | None = None
        self.last_quotes: dict[str, Quote] = {}

    # -- configuration -----------------------------------------------------------------------

    def set_prev_closes(self, closes: Mapping[str, float]) -> None:
        """Previous session closes (raw) by instrument key, from the daily history."""
        self._prev_close = dict(closes)

    @property
    def next_delay_s(self) -> float:
        """How long to wait before the next poll (backs off after failures)."""
        if self._failures == 0:
            return self._poll_interval_s
        return min(self._max_backoff_s, self._poll_interval_s * 2.0**self._failures)

    @property
    def stale_keys(self) -> frozenset[str]:
        return frozenset(self._stale_since)

    # -- polling -----------------------------------------------------------------------------

    async def poll(self) -> PollResult:
        started = time.perf_counter()
        if self._inflight is not None and not self._inflight.done():
            # A timed-out download is still running in its thread: never stack another one.
            return self._failed("previous download still running", started)
        tickers = sorted(self._by_ticker)
        self._inflight = asyncio.ensure_future(asyncio.to_thread(self._fetch, tickers))
        try:
            raw = await asyncio.wait_for(asyncio.shield(self._inflight), self._timeout_s)
        except TimeoutError:
            return self._failed(f"download timed out after {self._timeout_s:.0f}s", started)
        except Exception as exc:  # network, parsing, yfinance internals
            return self._failed(f"{type(exc).__name__}: {exc}", started)
        if not raw:
            return self._failed("no symbol returned data (rate-limited or down?)", started)
        return self._accept(raw, started)

    def _fetch(self, tickers: list[str]) -> dict[str, RawQuote]:
        """Runs in a worker thread: download and reduce to one raw quote per instrument key."""
        frame = self._download(
            tickers,
            period="1d",
            interval="1m",
            auto_adjust=False,
            group_by="ticker",
            threads=True,
            progress=False,
        )
        return parse_minute_frame(frame, self._by_ticker)

    def _accept(self, raw: dict[str, RawQuote], started: float) -> PollResult:
        now = self._clock.now()
        self._mark_feed_up(now)
        quotes: dict[str, Quote] = {}
        rejected: list[str] = []
        for key, item in sorted(raw.items()):
            item = item.with_prev_close(self._prev_close.get(key))
            reason = self._validator.check(item)
            if reason is not None:
                rejected.append(key)
                self._sink.emit(
                    QuoteRejected(
                        instrument_key=key, source=SOURCE, reason=reason, raw=item.as_json()
                    ),
                    source="marketdata",
                )
                continue
            quote = item.to_quote(receipt_ts=now, source=SOURCE)
            self._validator.accept(quote)
            quotes[key] = quote
        missing = tuple(sorted(set(self._by_ticker.values()) - set(raw)))
        self._update_symbol_staleness(now, quotes, missing)
        if self._tape is not None and quotes:
            self._tape.add_quotes(quotes.values())
            self._tape.flush()
        self.last_quotes.update(quotes)
        return PollResult(
            ok=True,
            quotes=quotes,
            missing=missing,
            rejected=tuple(rejected),
            latency_s=time.perf_counter() - started,
        )

    # -- staleness and backoff -----------------------------------------------------------------

    def _failed(self, error: str, started: float) -> PollResult:
        self._failures += 1
        now = self._clock.now()
        logger.warning("YFinance poll failed (%d in a row): %s", self._failures, error)
        if self._feed_down_since is None:
            self._feed_down_since = now
            self._sink.emit(
                FeedStale(
                    source=SOURCE,
                    instrument_keys=tuple(sorted(self._by_ticker.values())),
                    reason=error,
                ),
                source="marketdata",
            )
        return PollResult(ok=False, quotes={}, error=error, latency_s=time.perf_counter() - started)

    def _mark_feed_up(self, now: datetime) -> None:
        self._failures = 0
        if self._feed_down_since is not None:
            down_for = (now - self._feed_down_since).total_seconds()
            self._feed_down_since = None
            self._sink.emit(FeedRecovered(source=SOURCE, stale_for_s=down_for), source="marketdata")

    def _update_symbol_staleness(
        self, now: datetime, quotes: Mapping[str, Quote], missing: Sequence[str]
    ) -> None:
        if not self._market_open(now):
            stale_now: set[str] = set(missing)  # off-hours, old quotes are expected
        else:
            stale_now = set(missing) | {
                key for key, q in quotes.items() if q.age_seconds(now) > self._stale_after_s
            }
        newly = sorted(stale_now - set(self._stale_since))
        recovered = sorted(set(self._stale_since) - stale_now)
        no_data = tuple(k for k in newly if k not in quotes)
        too_old = tuple(k for k in newly if k in quotes)
        if no_data:
            self._sink.emit(
                FeedStale(source=SOURCE, instrument_keys=no_data, reason="no data"),
                source="marketdata",
            )
        if too_old:
            self._sink.emit(
                FeedStale(
                    source=SOURCE,
                    instrument_keys=too_old,
                    age_s=max(quotes[k].age_seconds(now) for k in too_old),
                    reason=f"older than {self._stale_after_s:.0f}s",
                ),
                source="marketdata",
            )
        for key in newly:
            self._stale_since[key] = now
        if recovered:
            longest = max((now - self._stale_since[k]).total_seconds() for k in recovered)
            self._sink.emit(
                FeedRecovered(source=SOURCE, instrument_keys=tuple(recovered), stale_for_s=longest),
                source="marketdata",
            )
            for key in recovered:
                del self._stale_since[key]


def parse_minute_frame(frame: Any, by_ticker: Mapping[str, str]) -> dict[str, RawQuote]:
    """Reduce a batched ``yf.download(group_by="ticker")`` 1-minute frame to raw quotes.

    For each ticker: the last bar with a close gives ``ltp`` and ``exchange_ts`` (the bar's
    timestamp, tz-aware IST); ``volume_cum`` is the day's volume so far (sum of the bars).
    """
    out: dict[str, RawQuote] = {}
    if frame is None or getattr(frame, "empty", True):
        return out
    available = set(frame.columns.get_level_values(0)) if frame.columns.nlevels == 2 else set()
    for ticker, key in by_ticker.items():
        if ticker not in available:
            continue
        sub = frame[ticker].dropna(subset=["Close"])
        if sub.empty:
            continue
        last_ts = sub.index[-1]
        if last_ts.tzinfo is None:
            raise ValueError(f"{ticker}: Yahoo returned a naive timestamp; refusing to guess")
        volume = sub["Volume"].fillna(0).sum()
        out[key] = RawQuote(
            instrument_key=key,
            ltp=float(sub["Close"].iloc[-1]),
            volume_cum=int(volume) if math.isfinite(float(volume)) else 0,
            exchange_ts=last_ts.to_pydatetime(),
        )
    return out
