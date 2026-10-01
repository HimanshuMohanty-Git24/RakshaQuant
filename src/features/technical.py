"""
Technical features of one instrument on its **settled** daily bars (plan M5.1).

:func:`compute_features` is the single feature path for live decisions, the shadow ledger and the
backtest. The input is dividend-adjusted daily OHLCV, oldest first, ending at the last *settled*
session - never the still-forming bar. Warm-up values (NaN/inf) become ``None``, so a strategy
can never compare against NaN.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, ADXIndicator, EMAIndicator, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

EMA_PERIODS = (9, 21, 55)
SMA_PERIODS = (20, 50, 200)
ADV_WINDOW = 20
_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Features:
    instrument_key: str
    bar_date: date
    bars: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    prev_close: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    adx_14: float | None = None
    plus_di_14: float | None = None
    minus_di_14: float | None = None
    atr_14: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_percent: float | None = None
    ema: Mapping[int, float] = field(default_factory=dict)
    sma: Mapping[int, float] = field(default_factory=dict)
    adv20_shares: float | None = None
    adv20_inr: float | None = None
    sigma_daily: float | None = None  # stdev of daily log returns over 20 sessions

    @property
    def atr_pct(self) -> float | None:
        return None if self.atr_14 is None or self.close <= 0 else self.atr_14 / self.close

    @property
    def bb_width(self) -> float | None:
        if self.bb_upper is None or self.bb_lower is None or not self.bb_middle:
            return None
        return (self.bb_upper - self.bb_lower) / self.bb_middle

    def as_dict(self) -> dict[str, float | int | str | None]:
        """Flat, JSON-ready (``ema_21``, ``sma_200``, ...), for lineage and advisors."""
        out: dict[str, float | int | str | None] = {
            "instrument_key": self.instrument_key,
            "bar_date": self.bar_date.isoformat(),
            "bars": self.bars,
        }
        for name in (
            "open", "high", "low", "close", "volume", "prev_close", "rsi_14", "macd",
            "macd_signal", "macd_hist", "adx_14", "plus_di_14", "minus_di_14", "atr_14",
            "bb_upper", "bb_middle", "bb_lower", "bb_percent", "adv20_shares", "adv20_inr",
            "sigma_daily",
        ):  # fmt: skip
            out[name] = getattr(self, name)
        out.update({f"ema_{p}": v for p, v in sorted(self.ema.items())})
        out.update({f"sma_{p}": v for p, v in sorted(self.sma.items())})
        return out


def compute_features(frame: pd.DataFrame, instrument_key: str) -> Features:
    """Features of the last bar of ``frame`` (open/high/low/close/volume, indexed by date)."""
    missing = [c for c in _COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    if frame.empty:
        raise ValueError(f"no bars for {instrument_key}")
    df = frame[list(_COLUMNS)].astype(float)
    n = len(df)
    close, high, low = df["close"], df["high"], df["low"]
    last = df.iloc[-1]

    def tail(series: Any) -> float | None:
        return _finite(series.iloc[-1])

    rsi = tail(RSIIndicator(close, window=14).rsi()) if n >= 14 else None
    macd_line = macd_sig = macd_hist = None
    if n >= 26:
        macd = MACD(close, window_fast=12, window_slow=26, window_sign=9)
        macd_line, macd_sig, macd_hist = (
            tail(macd.macd()), tail(macd.macd_signal()), tail(macd.macd_diff())
        )  # fmt: skip
    adx = plus_di = minus_di = None
    if n >= 28:  # ADX needs 2x its window to leave warm-up
        ind = ADXIndicator(high, low, close, window=14)
        adx, plus_di, minus_di = tail(ind.adx()), tail(ind.adx_pos()), tail(ind.adx_neg())
    atr = None
    if n >= 15:  # ta reports 0 (not NaN) during the ATR warm-up
        atr = tail(AverageTrueRange(high, low, close, window=14).average_true_range())
    bb_upper = bb_middle = bb_lower = bb_pct = None
    if n >= 20:
        bb = BollingerBands(close, window=20, window_dev=2)
        bb_upper, bb_middle = tail(bb.bollinger_hband()), tail(bb.bollinger_mavg())
        bb_lower, bb_pct = tail(bb.bollinger_lband()), tail(bb.bollinger_pband())
    ema: dict[int, float] = {}
    for period in (p for p in EMA_PERIODS if n >= p):
        if (value := tail(EMAIndicator(close, window=period).ema_indicator())) is not None:
            ema[period] = value
    sma: dict[int, float] = {}
    for period in (p for p in SMA_PERIODS if n >= p):
        if (value := tail(SMAIndicator(close, window=period).sma_indicator())) is not None:
            sma[period] = value

    adv_shares = adv_inr = sigma = None
    if n >= ADV_WINDOW:
        window = df.iloc[-ADV_WINDOW:]
        adv_shares = _finite(window["volume"].mean())
        adv_inr = _finite((window["volume"] * window["close"]).mean())
    if n > ADV_WINDOW:
        returns = (close / close.shift(1)).apply(_log).iloc[-ADV_WINDOW:]
        sigma = _finite(returns.std(ddof=1))

    return Features(
        instrument_key=instrument_key,
        bar_date=_as_date(df.index[-1]),
        bars=n,
        open=float(last["open"]),
        high=float(last["high"]),
        low=float(last["low"]),
        close=float(last["close"]),
        volume=float(last["volume"]),
        prev_close=_finite(close.iloc[-2]) if n >= 2 else None,
        rsi_14=rsi,
        macd=macd_line,
        macd_signal=macd_sig,
        macd_hist=macd_hist,
        adx_14=adx,
        plus_di_14=plus_di,
        minus_di_14=minus_di,
        atr_14=atr,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        bb_percent=bb_pct,
        ema=ema,
        sma=sma,
        adv20_shares=adv_shares,
        adv20_inr=adv_inr,
        sigma_daily=sigma,
    )


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _log(value: float) -> float:
    return math.log(value) if value and value > 0 and math.isfinite(value) else math.nan


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    ts: date = pd.Timestamp(value).date()  # pandas is untyped here
    return ts
