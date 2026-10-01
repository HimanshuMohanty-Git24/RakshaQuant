"""Plan M2.3: daily history with dividend back-adjustment, settled bars only, no synthetic data."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.domain.clock import ReplayClock
from src.domain.events import Alert
from src.domain.sink import RecordingSink
from src.marketdata.history import (
    alert_failures,
    dividend_factors,
    fetch_daily_history,
    parse_daily_frame,
    record_history,
)
from src.store.tape import TapeWriter, read_bars

TODAY = date(2026, 10, 5)
TICKERS = {"NSE:EQ:INFY": "INFY.NS", "NSE:EQ:TCS": "TCS.NS"}


def daily_frame(
    tickers: list[str],
    days: int = 80,
    *,
    dividends: dict[int, float] | None = None,
    splits: dict[int, float] | None = None,
    end: str = "2026-10-05",
) -> pd.DataFrame:
    """Shaped like ``yf.download(interval="1d", actions=True, group_by="ticker")`` (naive dates)."""
    index = pd.bdate_range(end=end, periods=days)
    cols: dict[tuple[str, str], Any] = {}
    for n, t in enumerate(tickers):
        close = 100.0 + n * 10 + np.arange(days, dtype=float)
        div = np.zeros(days)
        for i, d in (dividends or {}).items():
            div[i] = d
        spl = np.zeros(days)
        for i, s in (splits or {}).items():
            spl[i] = s
        for field, values in (
            ("Open", close - 0.5),
            ("High", close + 1.0),
            ("Low", close - 1.0),
            ("Close", close),
            ("Adj Close", close),
            ("Volume", np.full(days, 5000.0)),
            ("Dividends", div),
            ("Stock Splits", spl),
        ):
            cols[(t, field)] = values
    return pd.DataFrame(cols, index=index)


def test_dividend_factor_math():
    close = np.array([100.0, 102.0, 104.0, 106.0])
    divs = np.array([0.0, 0.0, 5.1, 0.0])  # ex-date index 2; prior close 102 -> 0.95
    np.testing.assert_allclose(dividend_factors(close, divs), [0.95, 0.95, 1.0, 1.0])
    two = np.array([0.0, 2.0, 0.0, 5.3])  # ex 3 (prev 104 -> 0.949..), ex 1 (prev 100 -> 0.98)
    f = dividend_factors(close, two)
    np.testing.assert_allclose(f, [0.98 * (1 - 5.3 / 104), 1 - 5.3 / 104, 1 - 5.3 / 104, 1.0])


def test_dividend_on_the_first_bar_is_ignored_and_bad_data_is_refused():
    np.testing.assert_allclose(
        dividend_factors(np.array([100.0, 101.0]), np.array([3.0, 0.0])), 1.0
    )
    with pytest.raises(ValueError, match="cannot adjust"):
        dividend_factors(np.array([1.0, 2.0]), np.array([0.0, 5.0]))


def test_adjusted_series_scales_only_prices_before_the_ex_date():
    frame = daily_frame(["INFY.NS"], dividends={70: 8.5})
    s = parse_daily_frame(frame, {"k": "INFY.NS"}, settled_before=date(2026, 10, 6)).series["k"]
    raw, adj = s.raw(), s.adjusted()
    expected = 1 - 8.5 / raw["close"].iloc[69]
    assert adj["close"].iloc[69] == pytest.approx(raw["close"].iloc[69] * expected)
    assert adj["close"].iloc[70:].tolist() == raw["close"].iloc[70:].tolist()
    assert adj["volume"].tolist() == raw["volume"].tolist()  # dividends never touch volume


def test_splits_are_not_adjusted_again():
    """Yahoo's Close is already split-adjusted (verified live); a second adjustment would
    double count, so the split column must not move the factor."""
    frame = daily_frame(["INFY.NS"], splits={40: 2.0})
    s = parse_daily_frame(frame, {"k": "INFY.NS"}, settled_before=date(2026, 10, 6)).series["k"]
    assert (s.frame["factor"] == 1.0).all()
    assert s.frame["split"].iloc[40] == 2.0


def test_only_settled_bars_are_kept():
    frame = daily_frame(list(TICKERS.values()))
    result = parse_daily_frame(frame, TICKERS, settled_before=TODAY)
    s = result.series["NSE:EQ:INFY"]
    assert s.last_date == date(2026, 10, 2)  # Friday's bar; today's (forming) bar dropped
    assert result.prev_closes()["NSE:EQ:INFY"] == s.raw()["close"].iloc[-1]


def test_missing_and_short_histories_are_excluded_not_invented():
    frame = daily_frame(["INFY.NS"], days=80)
    short = daily_frame(["TCS.NS"], days=20)
    both = pd.concat([frame, short], axis=1)
    result = parse_daily_frame(
        both, {**TICKERS, "NSE:EQ:ITC": "ITC.NS"}, settled_before=TODAY, min_bars=60
    )
    assert set(result.series) == {"NSE:EQ:INFY"}
    assert result.failed["NSE:EQ:TCS"].startswith("only 19 settled bars")
    assert result.failed["NSE:EQ:ITC"] == "no data returned"


def test_inconsistent_ohlc_is_reordered_and_counted():
    frame = daily_frame(["INFY.NS"])
    frame.loc[frame.index[10], ("INFY.NS", "High")] = 50.0  # below the close
    result = parse_daily_frame(frame, {"k": "INFY.NS"}, settled_before=TODAY)
    assert result.repaired == {"k": 1}
    bars = result.series["k"].bars(adjusted=False)  # Bar validation would reject bad OHLC
    assert bars[10].high == max(bars[10].open, bars[10].close, 50.0)


async def test_fetch_uses_one_batched_call_with_actions():
    calls: list[dict[str, Any]] = []

    def download(tickers: list[str], **kw: Any) -> pd.DataFrame:
        calls.append({"tickers": tickers, **kw})
        return daily_frame(tickers)

    result = await fetch_daily_history(TICKERS, settled_before=TODAY, download=download)
    assert set(result.series) == set(TICKERS) and not result.failed
    (call,) = calls
    assert call["tickers"] == ["INFY.NS", "TCS.NS"]
    assert (call["period"], call["interval"], call["actions"], call["auto_adjust"]) == (
        "1y",
        "1d",
        True,
        False,
    )


async def test_a_failed_download_fails_every_symbol_without_fallback():
    def download(*_: Any, **__: Any) -> pd.DataFrame:
        raise ConnectionError("yahoo down")

    result = await fetch_daily_history(TICKERS, settled_before=TODAY, download=download)
    assert result.series == {}
    assert set(result.failed) == set(TICKERS)
    assert "ConnectionError" in result.failed["NSE:EQ:INFY"]


def test_failures_raise_alerts_and_bars_are_taped(tmp_path):
    frame = daily_frame(["INFY.NS"], dividends={70: 8.5})
    result = parse_daily_frame(frame, {**TICKERS}, settled_before=TODAY)
    sink = RecordingSink(ReplayClock(datetime(2026, 10, 5, 3, 0, tzinfo=UTC)))
    alert_failures(result, sink)
    (alert,) = sink.payloads(Alert)
    assert alert.key == "history_missing:NSE:EQ:TCS" and alert.level == "WARNING"

    record_history(result, tape=TapeWriter(tmp_path), recorded_on=TODAY)
    bars = read_bars(tmp_path, TODAY)
    assert len(bars) == 2 * len(result.series["NSE:EQ:INFY"].frame)
    assert {b.adjusted for b in bars} == {True, False}


def test_a_latest_bar_with_a_nan_close_is_dropped_and_reported_as_lagging():
    """Seen live on 2026-10-02: Yahoo's 1 Oct daily bar had open/volume but a NaN close."""
    frame = daily_frame(["INFY.NS", "TCS.NS"], end="2026-10-01")
    frame.loc[frame.index[-1], ("INFY.NS", "Close")] = np.nan
    result = parse_daily_frame(
        frame, TICKERS, settled_before=date(2026, 10, 2), expected_last=date(2026, 10, 1)
    )
    assert result.series["NSE:EQ:INFY"].last_date == date(2026, 9, 30)  # never invented
    assert result.lagging == {"NSE:EQ:INFY": date(2026, 9, 30)}
    sink = RecordingSink(ReplayClock(datetime(2026, 10, 5, 3, 0, tzinfo=UTC)))
    alert_failures(result, sink)
    (alert,) = sink.payloads(Alert)
    assert alert.key == "history_lagging:NSE:EQ:INFY"
