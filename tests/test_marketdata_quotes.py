"""Plan M2.1 + M2.2: the batched YFinance poller and quote validation."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.domain.clock import ReplayClock
from src.domain.events import FeedRecovered, FeedStale, QuoteRejected
from src.domain.sink import RecordingSink
from src.domain.types import Instrument, MarketDataSource
from src.marketdata.validation import QuoteValidator, RawQuote
from src.marketdata.yfinance_source import YFinanceQuoteSource, parse_minute_frame
from src.store.tape import TapeWriter, read_quotes

SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "ITC", "SBIN", "LT", "AXISBANK",
    "KOTAKBANK", "BHARTIARTL", "HINDUNILVR", "MARUTI", "TITAN", "SUNPHARMA",
]  # fmt: skip
INSTRUMENTS = [Instrument.nse_equity(s) for s in SYMBOLS]
SESSION_OPEN = pd.Timestamp("2026-10-05 09:15", tz="Asia/Kolkata")
# Yahoo lags ~15 min: at 10:00:30 IST the newest 1-minute bar is ~09:45.
NOW = datetime(2026, 10, 5, 4, 30, 30, tzinfo=UTC)


def minute_frame(
    symbols: list[str], bars: int = 31, *, base: float = 1000.0, nan_for: set[str] | None = None
) -> pd.DataFrame:
    """A frame shaped like ``yf.download(group_by="ticker", interval="1m")``."""
    index = pd.date_range(SESSION_OPEN, periods=bars, freq="1min")
    columns: dict[tuple[str, str], Any] = {}
    for n, sym in enumerate(symbols):
        close = base + n + np.arange(bars) * 0.05
        if nan_for and sym in nan_for:
            close = np.full(bars, np.nan)
        for field, values in (
            ("Open", close),
            ("High", close + 0.5),
            ("Low", close - 0.5),
            ("Close", close),
            ("Adj Close", close),
            ("Volume", np.full(bars, 1000.0)),
        ):
            columns[(f"{sym}.NS", field)] = values
    return pd.DataFrame(columns, index=index)


class FakeDownload:
    def __init__(self, frames: list[Any] | None = None, *, delay_s: float = 0.0) -> None:
        self.frames = frames or [minute_frame(SYMBOLS)]
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    def __call__(self, tickers: list[str], **kwargs: Any) -> Any:
        self.calls.append({"tickers": tickers, **kwargs})
        if self.delay_s:
            time.sleep(self.delay_s)  # blocking on purpose: it must run off the event loop
        frame = self.frames[min(len(self.calls), len(self.frames)) - 1]
        if isinstance(frame, Exception):
            raise frame
        return frame


def source(download: FakeDownload, **kwargs: Any) -> tuple[YFinanceQuoteSource, RecordingSink]:
    clock = kwargs.pop("clock", ReplayClock(NOW))
    sink = RecordingSink(clock)
    return YFinanceQuoteSource(
        INSTRUMENTS, clock=clock, sink=sink, download=download, **kwargs
    ), sink


# --- parsing --------------------------------------------------------------------------------


def test_parse_takes_last_close_and_day_volume():
    frame = minute_frame(["INFY", "TCS"], bars=10)
    raw = parse_minute_frame(frame, {"INFY.NS": "NSE:EQ:INFY", "TCS.NS": "NSE:EQ:TCS"})
    infy = raw["NSE:EQ:INFY"]
    assert infy.ltp == pytest.approx(1000.0 + 9 * 0.05)
    assert infy.volume_cum == 10_000
    assert infy.exchange_ts == datetime(2026, 10, 5, 9, 24, tzinfo=infy.exchange_ts.tzinfo)
    assert infy.exchange_ts.utcoffset() == timedelta(hours=5, minutes=30)


def test_parse_skips_absent_and_all_nan_tickers():
    frame = minute_frame(["INFY", "TCS"], nan_for={"TCS"})
    by_ticker = {"INFY.NS": "k1", "TCS.NS": "k2", "NOPE.NS": "k3"}
    assert set(parse_minute_frame(frame, by_ticker)) == {"k1"}
    assert parse_minute_frame(pd.DataFrame(), by_ticker) == {}


# --- polling ----------------------------------------------------------------------------------


async def test_one_batched_call_for_fifteen_symbols_within_a_second(tmp_path):
    download = FakeDownload()
    src, _ = source(download, tape=TapeWriter(tmp_path))
    src.set_prev_closes({i.key: 1000.0 for i in INSTRUMENTS})
    started = time.perf_counter()
    result = await src.poll()
    elapsed = time.perf_counter() - started
    assert result.ok and len(result.quotes) == 15 and not result.missing
    assert elapsed <= 1.0, f"15-symbol mocked poll took {elapsed:.2f}s"
    (call,) = download.calls
    assert len(call["tickers"]) == 15
    assert call["period"] == "1d" and call["interval"] == "1m" and call["auto_adjust"] is False
    assert call["group_by"] == "ticker" and call["threads"] is True and call["progress"] is False

    q = result.quotes["NSE:EQ:INFY"]
    assert q.source is MarketDataSource.YFINANCE and q.is_delayed
    assert q.receipt_ts == NOW and q.prev_close == 1000.0
    assert q.exchange_ts == SESSION_OPEN + pd.Timedelta(minutes=30)
    assert read_quotes(tmp_path, NOW.date()) == sorted(
        result.quotes.values(), key=lambda x: x.instrument_key
    )


async def test_event_loop_stays_responsive_during_a_poll():
    download = FakeDownload([minute_frame(SYMBOLS, bars=375)], delay_s=0.5)
    src, _ = source(download)
    lags: list[float] = []

    async def probe() -> None:
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t0 - 0.01)

    task = asyncio.create_task(probe())
    result = await src.poll()
    task.cancel()
    assert result.ok and len(lags) >= 10
    assert max(lags) < 0.050, f"loop lag {max(lags) * 1000:.0f} ms during the poll"


async def test_failures_back_off_and_mark_the_feed_stale_once():
    download = FakeDownload(
        [RuntimeError("HTTP 429"), RuntimeError("HTTP 429"), minute_frame(SYMBOLS)]
    )
    src, sink = source(download)
    assert src.next_delay_s == 60
    first = await src.poll()
    assert not first.ok and "429" in (first.error or "")
    assert src.next_delay_s == 120
    await src.poll()
    assert src.next_delay_s == 240
    stale = sink.payloads(FeedStale)
    assert len(stale) == 1 and len(stale[0].instrument_keys) == 15
    assert (await src.poll()).ok
    assert src.next_delay_s == 60
    assert len(sink.payloads(FeedRecovered)) == 1


async def test_backoff_is_capped():
    src, _ = source(FakeDownload([RuntimeError("down")]), max_backoff_s=300)
    for _ in range(8):
        await src.poll()
    assert src.next_delay_s == 300


async def test_all_empty_frame_counts_as_a_failed_poll():
    src, sink = source(FakeDownload([minute_frame(SYMBOLS, nan_for=set(SYMBOLS))]))
    result = await src.poll()
    assert not result.ok and "no symbol returned data" in (result.error or "")
    assert len(sink.payloads(FeedStale)) == 1


async def test_timeouts_never_stack_download_threads():
    download = FakeDownload(delay_s=0.6)
    src, _ = source(download, timeout_s=0.1)
    timed_out = await src.poll()
    assert not timed_out.ok and "timed out" in (timed_out.error or "")
    blocked = await src.poll()
    assert "still running" in (blocked.error or "") and len(download.calls) == 1
    await asyncio.sleep(0.7)
    download.delay_s = 0
    assert (await src.poll()).ok


async def test_stale_and_missing_symbols_while_open():
    late = NOW + timedelta(minutes=30)  # newest bar 09:45 IST is now 45 min old
    clock = ReplayClock(late)
    frame = minute_frame([s for s in SYMBOLS if s != "ITC"])
    src, sink = source(FakeDownload([frame, minute_frame(SYMBOLS, bars=60)]), clock=clock,
                       market_open=lambda _ts: True)  # fmt: skip
    result = await src.poll()
    assert result.ok and result.missing == ("NSE:EQ:ITC",)
    assert src.stale_keys == frozenset(i.key for i in INSTRUMENTS)
    by_reason = {p.reason: p for p in sink.payloads(FeedStale)}
    assert by_reason["no data"].instrument_keys == ("NSE:EQ:ITC",)
    old = by_reason["older than 1200s"]
    assert len(old.instrument_keys) == 14 and old.age_s == pytest.approx(45 * 60 + 30)
    await src.poll()  # 60 bars: newest 10:14 IST, 1 min old -> everything fresh again
    assert src.stale_keys == frozenset()
    (recovered,) = sink.payloads(FeedRecovered)
    assert len(recovered.instrument_keys) == 15


async def test_old_quotes_are_not_stale_when_the_market_is_closed():
    clock = ReplayClock(NOW + timedelta(hours=12))
    src, sink = source(FakeDownload(), clock=clock, market_open=lambda _ts: False)
    assert (await src.poll()).ok
    assert src.stale_keys == frozenset() and not sink.payloads(FeedStale)


# --- validation (M2.2) ----------------------------------------------------------------------


def raw(ltp: float, volume: int = 1000, prev: float | None = 100.0, minute: int = 0) -> RawQuote:
    ts = datetime(2026, 10, 5, 9, 30 + minute, tzinfo=UTC)
    return RawQuote("NSE:EQ:INFY", ltp, volume, ts, prev)


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        (raw(math.nan), "non_finite_price"),
        (raw(math.inf), "non_finite_price"),
        (raw(0.0), "non_positive_price"),
        (raw(-5.0), "non_positive_price"),
        (raw(122.5), "out_of_band"),  # band 20% + 2% tolerance
        (raw(77.0), "out_of_band"),
        (raw(121.9), None),
        (raw(150.0, prev=None), None),  # no reference close: band unknown, accept
    ],
)
def test_validation_rules(quote, reason):
    assert QuoteValidator().check(quote) == reason


def test_band_comes_from_the_instrument():
    validator = QuoteValidator(lambda key: 5.0)
    assert validator.check(raw(106.9)) is None
    assert validator.check(raw(107.1)) == "out_of_band"


def test_volume_must_not_decrease_within_a_session():
    v = QuoteValidator()
    v.accept(raw(101, volume=5000).to_quote(receipt_ts=NOW, source=MarketDataSource.YFINANCE))
    assert v.check(raw(101, volume=4999, minute=1)) == "volume_decreased"
    assert v.check(raw(101, volume=5000, minute=1)) is None
    next_day = RawQuote("NSE:EQ:INFY", 101, 10, datetime(2026, 10, 6, 4, 0, tzinfo=UTC), 100.0)
    assert v.check(next_day) is None  # a new session starts from zero


async def test_rejected_quotes_are_dropped_with_an_event_and_not_taped(tmp_path):
    frame = minute_frame(SYMBOLS)
    src, sink = source(FakeDownload([frame]), tape=TapeWriter(tmp_path))
    closes = {i.key: 1000.0 for i in INSTRUMENTS}
    closes["NSE:EQ:INFY"] = 500.0  # INFY's ~1002 is a 100% "move": a bad tick
    src.set_prev_closes(closes)
    result = await src.poll()
    assert result.rejected == ("NSE:EQ:INFY",) and "NSE:EQ:INFY" not in result.quotes
    (event,) = sink.payloads(QuoteRejected)
    assert event.reason == "out_of_band" and event.raw["prev_close"] == 500.0
    assert "NSE:EQ:INFY" not in {q.instrument_key for q in read_quotes(tmp_path, NOW.date())}
