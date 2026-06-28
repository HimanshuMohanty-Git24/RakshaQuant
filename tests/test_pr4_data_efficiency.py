"""
Tests for PR-4 data efficiency & fusion:

- Volume double-count fix + IST-aware bar bucketing in HistoryManager.append_quote.
- Forming-bar exclusion in HistoryManager.get_history (look-ahead fix).
- NaN/inf sanitization in indicator calculation.
- IndicatorCache hit/miss behaviour.
- MarketQuote staleness helpers.
"""

import math
from datetime import timedelta

import numpy as np
import pandas as pd

from src.market.history_manager import HistoryManager
from src.market.indicators import (
    Timeframe,
    _safe_float,
    calculate_indicators,
    get_indicator_cache,
    reset_indicator_cache,
)
from src.market.manager import MarketQuote
from src.utils.market_time import now_ist


def _ohlcv(dates, base=100.0):
    n = len(dates)
    closes = base + np.linspace(0, n * 0.1, n)
    return pd.DataFrame(
        {
            "open": closes - 0.2,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.full(n, 100_000),
        },
        index=dates,
    )


# ---------------------------------------------------------------------------
# HistoryManager.append_quote — volume double-count fix
# ---------------------------------------------------------------------------

def test_append_quote_does_not_sum_cumulative_volume():
    hm = HistoryManager(symbols=[])
    today = pd.Timestamp(now_ist().date())
    hm._history["X"] = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [100.0]},
        index=[today],
    )

    ts = now_ist()
    hm.append_quote("X", open_price=100, high=102, low=98, close=101, volume=5000, timestamp=ts)
    hm.append_quote("X", open_price=100, high=103, low=97, close=102, volume=5000, timestamp=ts)

    df = hm.get_history("X")
    # Cumulative daily volume is kept (max), NOT summed (would be 10,100).
    assert df.iloc[-1]["volume"] == 5000
    # Same-day update folds high/low/close into the one bar.
    assert df.iloc[-1]["high"] == 103
    assert df.iloc[-1]["low"] == 97
    assert df.iloc[-1]["close"] == 102
    assert len(df) == 1


# ---------------------------------------------------------------------------
# HistoryManager.get_history — forming-bar exclusion
# ---------------------------------------------------------------------------

def test_get_history_excludes_forming_bar():
    hm = HistoryManager(symbols=[])
    today = now_ist().date()
    dates = [
        pd.Timestamp(today - timedelta(days=2)),
        pd.Timestamp(today - timedelta(days=1)),
        pd.Timestamp(today),  # the still-forming bar
    ]
    hm._history["X"] = _ohlcv(dates)

    full = hm.get_history("X", include_forming=True)
    settled = hm.get_history("X", include_forming=False)

    assert len(full) == 3
    assert len(settled) == 2
    assert settled.index[-1].date() == today - timedelta(days=1)


def test_get_history_never_empties_when_all_today():
    hm = HistoryManager(symbols=[])
    hm._history["X"] = _ohlcv([pd.Timestamp(now_ist().date())])
    # Dropping the forming bar would empty it -> fall back to full history.
    settled = hm.get_history("X", include_forming=False)
    assert len(settled) == 1


# ---------------------------------------------------------------------------
# NaN/inf sanitization
# ---------------------------------------------------------------------------

def test_safe_float():
    assert _safe_float(1.5) == 1.5
    assert _safe_float(float("nan")) is None
    assert _safe_float(float("inf")) is None
    assert _safe_float(None) is None
    assert _safe_float("not a number") is None


def test_calculate_indicators_emits_no_nan():
    dates = pd.date_range("2024-01-01", periods=60, freq="D")
    result = calculate_indicators(_ohlcv(dates), "X", timeframe=Timeframe.D1)

    def _all_numbers(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from _all_numbers(v)
        elif isinstance(obj, (int, float)):
            yield obj

    for number in _all_numbers(result.to_dict()):
        assert not (isinstance(number, float) and math.isnan(number)), "indicator emitted NaN"


# ---------------------------------------------------------------------------
# IndicatorCache
# ---------------------------------------------------------------------------

def test_indicator_cache_hits_on_identical_data():
    reset_indicator_cache()
    cache = get_indicator_cache()
    dates = pd.date_range("2024-01-01", periods=60, freq="D")
    df = _ohlcv(dates)

    first = cache.get_or_compute(df, "X", timeframe=Timeframe.D1)
    second = cache.get_or_compute(df, "X", timeframe=Timeframe.D1)

    assert second is first  # served from cache
    assert cache.hits == 1
    assert cache.misses == 1


def test_indicator_cache_misses_when_last_close_changes():
    reset_indicator_cache()
    cache = get_indicator_cache()
    dates = pd.date_range("2024-01-01", periods=60, freq="D")
    df = _ohlcv(dates)
    cache.get_or_compute(df, "X", timeframe=Timeframe.D1)

    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("close")] = df2.iloc[-1]["close"] + 5  # forming-bar move
    cache.get_or_compute(df2, "X", timeframe=Timeframe.D1)

    assert cache.misses == 2


# ---------------------------------------------------------------------------
# MarketQuote staleness
# ---------------------------------------------------------------------------

def _quote(ts):
    return MarketQuote(
        symbol="X", last_price=100.0, open=100.0, high=101.0, low=99.0, close=99.5,
        change=0.5, change_percent=0.5, volume=1000, is_live=False, timestamp=ts,
    )


def test_quote_age_and_staleness():
    stale = _quote(now_ist() - timedelta(seconds=100))
    assert stale.age_seconds >= 100
    assert stale.is_stale(60) is True
    assert stale.is_stale(200) is False
    assert stale.is_stale(0) is False  # disabled


def test_fresh_quote_not_stale():
    fresh = _quote(now_ist())
    assert fresh.age_seconds < 5
    assert fresh.is_stale(60) is False
