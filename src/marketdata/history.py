"""
Daily history (plan M2.3): one batched ``yf.download(period="1y", interval="1d",
auto_adjust=False, actions=True)`` per day, pre-open.

Two price series per instrument:

* **raw** — Yahoo's ``Close``/``Open``/... with ``auto_adjust=False``. Verified 2026-10-02: Yahoo
  has already applied *splits* to these (no jump across the 10 NIFTY 50 splits of 2024–26), but
  not dividends. This is the current share basis, consistent with live quotes, so **prices and
  stops use raw**.
* **adjusted** — raw × a back-adjustment factor built from the ``Dividends`` column only (splits
  are already in raw; adjusting them again would double count). For each ex-date ``e`` the bars
  before it scale by ``1 - D_e / C_{e-1}``. Verified to match Yahoo's ``Adj Close`` to 1e-7 on all
  48 dividend-paying NIFTY 50 names. **Indicators use adjusted**; an ATR computed on it is applied
  as a distance to the raw fill.

No synthetic history: a symbol that fails or is too short is excluded and reported in
``HistoryResult.failed`` (the caller raises an alert). Only settled bars are kept (dates before
``settled_before``).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from src.domain.events import Alert
from src.domain.sink import EventSink
from src.domain.types import Bar, MarketDataSource, Timeframe
from src.store.tape import TapeWriter

DEFAULT_MIN_BARS = 60
_OHLC = ("open", "high", "low", "close")

Downloader = Callable[..., Any]


def _default_download(*args: Any, **kwargs: Any) -> Any:
    import yfinance as yf

    return yf.download(*args, **kwargs)


def dividend_factors(close: np.ndarray, dividends: np.ndarray) -> np.ndarray:
    """Back-adjustment factor per bar: the product of ``1 - D_e / C_{e-1}`` over later ex-dates.

    The newest bar's factor is 1. A dividend on the first bar cannot be adjusted (no prior close)
    and is ignored.
    """
    if close.shape != dividends.shape:
        raise ValueError("close and dividends must be the same length")
    factors = np.ones(len(close))
    running = 1.0
    for i in range(len(close) - 1, 0, -1):
        factors[i] = running
        d = dividends[i]
        if d > 0:
            prev = close[i - 1]
            if not (math.isfinite(prev) and prev > d):
                raise ValueError(f"cannot adjust a dividend of {d} against a prior close of {prev}")
            running *= 1.0 - d / prev
    if len(close):
        factors[0] = running
    return factors


@dataclass(frozen=True)
class DailySeries:
    """One instrument's settled daily bars, indexed by session date (oldest first)."""

    instrument_key: str
    frame: pd.DataFrame  # open/high/low/close/volume (raw), dividend, split, factor

    def raw(self) -> pd.DataFrame:
        return self.frame[[*_OHLC, "volume"]].copy()

    def adjusted(self) -> pd.DataFrame:
        out = self.frame[[*_OHLC, "volume"]].copy()
        for col in _OHLC:
            out[col] = out[col] * self.frame["factor"]
        return out

    @property
    def last_date(self) -> date:
        return _as_date(self.frame.index[-1])

    @property
    def last_close(self) -> float:
        """The latest settled raw close (yesterday's close during a session)."""
        return float(self.frame["close"].iloc[-1])

    def bars(self, *, adjusted: bool) -> list[Bar]:
        data = self.adjusted() if adjusted else self.raw()
        return [
            Bar(
                instrument_key=self.instrument_key,
                timeframe=Timeframe.D1,
                session_date=_as_date(ts),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=int(row.volume),
                is_settled=True,
                adjusted=adjusted,
                source=MarketDataSource.YFINANCE,
            )
            for ts, row in zip(data.index, data.itertuples(index=False), strict=True)
        ]


@dataclass(frozen=True)
class HistoryResult:
    series: dict[str, DailySeries]
    failed: dict[str, str] = field(default_factory=dict)  # instrument key -> reason
    repaired: dict[str, int] = field(default_factory=dict)  # key -> rows with OHLC re-ordered
    # key -> last settled date, for series that end before the expected previous session
    # (Yahoo sometimes leaves the latest daily close NaN for hours after the close).
    lagging: dict[str, date] = field(default_factory=dict)

    def prev_closes(self) -> dict[str, float]:
        return {key: s.last_close for key, s in self.series.items()}


async def fetch_daily_history(
    tickers: Mapping[str, str],
    *,
    settled_before: date,
    download: Downloader | None = None,
    timeout_s: float = 60.0,
    period: str = "1y",
    min_bars: int = DEFAULT_MIN_BARS,
    expected_last: date | None = None,
) -> HistoryResult:
    """Fetch daily bars for ``{instrument_key: yahoo_ticker}`` in one batched call.

    A failed download fails every symbol (the caller alerts); it never falls back to anything.
    """
    fetch = download or _default_download

    def work() -> HistoryResult:
        frame = fetch(
            sorted(set(tickers.values())),
            period=period,
            interval="1d",
            auto_adjust=False,
            actions=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
        return parse_daily_frame(
            frame,
            tickers,
            settled_before=settled_before,
            min_bars=min_bars,
            expected_last=expected_last,
        )

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout_s)
    except Exception as exc:
        reason = f"download failed: {type(exc).__name__}: {exc}"
        return HistoryResult(series={}, failed={key: reason for key in tickers})


def parse_daily_frame(
    frame: Any,
    tickers: Mapping[str, str],
    *,
    settled_before: date,
    min_bars: int = DEFAULT_MIN_BARS,
    expected_last: date | None = None,
) -> HistoryResult:
    """Parse a batched daily frame. ``expected_last`` is the last session that should be settled
    (from the exchange calendar); series ending earlier are reported in ``lagging``."""
    series: dict[str, DailySeries] = {}
    failed: dict[str, str] = {}
    repaired: dict[str, int] = {}
    empty = frame is None or getattr(frame, "empty", True)
    available = set() if empty else set(frame.columns.get_level_values(0))
    for key, ticker in tickers.items():
        if ticker not in available:
            failed[key] = "no data returned"
            continue
        sub = frame[ticker].dropna(subset=["Close"])
        sub = sub[[_as_date(ts) < settled_before for ts in sub.index]]
        if len(sub) < min_bars:
            failed[key] = f"only {len(sub)} settled bars (need {min_bars})"
            continue
        out = pd.DataFrame(
            {
                "open": sub["Open"].fillna(sub["Close"]).astype(float),
                "high": sub["High"].fillna(sub["Close"]).astype(float),
                "low": sub["Low"].fillna(sub["Close"]).astype(float),
                "close": sub["Close"].astype(float),
                "volume": sub["Volume"].fillna(0).astype("int64"),
                "dividend": _column(sub, "Dividends"),
                "split": _column(sub, "Stock Splits"),
            },
            index=pd.DatetimeIndex([pd.Timestamp(_as_date(ts)) for ts in sub.index], name="date"),
        )
        # Yahoo occasionally reports a high below the close (or similar); restore the ordering
        # from the reported values rather than drop the bar. Nothing is invented.
        ohlc = out[list(_OHLC)]
        hi, lo = ohlc.max(axis=1), ohlc.min(axis=1)
        bad = int(((out["high"] != hi) | (out["low"] != lo)).sum())
        if bad:
            repaired[key] = bad
            out["high"], out["low"] = hi, lo
        try:
            out["factor"] = dividend_factors(out["close"].to_numpy(), out["dividend"].to_numpy())
        except ValueError as exc:
            failed[key] = f"bad dividend data: {exc}"
            continue
        series[key] = DailySeries(key, out)
    lagging = {
        key: s.last_date
        for key, s in series.items()
        if expected_last is not None and s.last_date < expected_last
    }
    return HistoryResult(series=series, failed=failed, repaired=repaired, lagging=lagging)


def record_history(result: HistoryResult, *, tape: TapeWriter, recorded_on: date) -> None:
    """Tape the raw and adjusted bars used for the day's decisions (for replay)."""
    for s in result.series.values():
        tape.add_bars(s.bars(adjusted=False), recorded_on=recorded_on)
        tape.add_bars(s.bars(adjusted=True), recorded_on=recorded_on)
    tape.flush()


def alert_failures(result: HistoryResult, sink: EventSink) -> None:
    """A WARNING alert per excluded instrument, and per instrument whose history lags."""
    for key, reason in sorted(result.failed.items()):
        sink.emit(
            Alert(
                level="WARNING",
                key=f"history_missing:{key}",
                message=f"{key} excluded today: no usable daily history ({reason})",
            ),
            source="marketdata",
        )
    for key, last in sorted(result.lagging.items()):
        sink.emit(
            Alert(
                level="WARNING",
                key=f"history_lagging:{key}",
                message=f"{key} daily history ends {last}, before the previous session",
            ),
            source="marketdata",
        )


def _column(sub: pd.DataFrame, name: str) -> pd.Series:
    if name in sub.columns:
        return sub[name].fillna(0.0).astype(float)
    return pd.Series(0.0, index=sub.index)


def _as_date(ts: Any) -> date:
    if isinstance(ts, pd.Timestamp):
        day: date = ts.date()  # pandas is untyped here
        return day
    if isinstance(ts, date):
        return ts
    raise TypeError(f"unexpected index value {ts!r}")
