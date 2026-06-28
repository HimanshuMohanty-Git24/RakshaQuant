"""
Technical Indicators Module

Computes various technical indicators for trading signal generation.
Uses the 'ta' library for standard indicators with custom extensions.
"""

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd
import ta
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator, SMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import VolumeWeightedAveragePrice

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> float | None:
    """
    Coerce a value to float, returning None for NaN/inf.

    The `ta` library emits NaN during the warm-up window (and on gaps). Passing NaN
    downstream silently distorts signal logic (`nan < 50` is always False) and produces
    invalid JSON ("NaN") in the agent context. Converting to None makes "not yet
    computed" explicit; the signal strategies already guard `if ind.x is None`.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


class Timeframe(Enum):
    """Candle timeframes."""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


@dataclass
class IndicatorConfig:
    """Configuration for indicator calculation."""
    
    # Moving Averages
    sma_periods: list[int] = None
    ema_periods: list[int] = None
    
    # Momentum
    rsi_period: int = 14
    stoch_k_period: int = 14
    stoch_d_period: int = 3
    
    # Trend
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    adx_period: int = 14
    
    # Volatility
    atr_period: int = 14
    bb_period: int = 20
    bb_std: int = 2
    
    def __post_init__(self):
        if self.sma_periods is None:
            self.sma_periods = [20, 50, 200]
        if self.ema_periods is None:
            self.ema_periods = [9, 21, 55]


@dataclass
class IndicatorResult:
    """Result of indicator calculations for a symbol."""
    
    symbol: str
    timeframe: Timeframe
    
    # Price data
    open: float
    high: float
    low: float
    close: float
    volume: int
    
    # Moving Averages
    sma: dict[int, float] = None  # period -> value
    ema: dict[int, float] = None
    
    # Momentum
    rsi: float = None
    stoch_k: float = None
    stoch_d: float = None
    
    # Trend
    macd: float = None
    macd_signal: float = None
    macd_histogram: float = None
    adx: float = None
    plus_di: float = None
    minus_di: float = None
    
    # Volatility
    atr: float = None
    bb_upper: float = None
    bb_middle: float = None
    bb_lower: float = None
    bb_percent: float = None
    
    # VWAP
    vwap: float = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "price": {
                "open": self.open,
                "high": self.high,
                "low": self.low,
                "close": self.close,
                "volume": self.volume,
            },
            "moving_averages": {
                "sma": self.sma,
                "ema": self.ema,
            },
            "momentum": {
                "rsi": self.rsi,
                "stoch_k": self.stoch_k,
                "stoch_d": self.stoch_d,
            },
            "trend": {
                "macd": self.macd,
                "macd_signal": self.macd_signal,
                "macd_histogram": self.macd_histogram,
                "adx": self.adx,
                "plus_di": self.plus_di,
                "minus_di": self.minus_di,
            },
            "volatility": {
                "atr": self.atr,
                "bb_upper": self.bb_upper,
                "bb_middle": self.bb_middle,
                "bb_lower": self.bb_lower,
                "bb_percent": self.bb_percent,
            },
            "vwap": self.vwap,
        }


def calculate_indicators(
    df: pd.DataFrame,
    symbol: str,
    timeframe: Timeframe = Timeframe.M5,
    config: IndicatorConfig | None = None,
) -> IndicatorResult:
    """
    Calculate all technical indicators for a symbol.
    
    Args:
        df: DataFrame with columns: open, high, low, close, volume
        symbol: Trading symbol
        timeframe: Candle timeframe
        config: Indicator configuration (uses defaults if None)
    
    Returns:
        IndicatorResult with all calculated indicators
    """
    if config is None:
        config = IndicatorConfig()
    
    # Ensure required columns exist
    required_cols = ["open", "high", "low", "close", "volume"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")
    
    # Get latest values
    latest = df.iloc[-1]
    
    # Calculate Moving Averages (skip NaN/warm-up values so membership guards hold)
    sma_values = {}
    for period in config.sma_periods:
        if len(df) >= period:
            sma = SMAIndicator(df["close"], window=period)
            value = _safe_float(sma.sma_indicator().iloc[-1])
            if value is not None:
                sma_values[period] = value

    ema_values = {}
    for period in config.ema_periods:
        if len(df) >= period:
            ema = EMAIndicator(df["close"], window=period)
            value = _safe_float(ema.ema_indicator().iloc[-1])
            if value is not None:
                ema_values[period] = value

    # Calculate RSI
    rsi = None
    if len(df) >= config.rsi_period:
        rsi_indicator = RSIIndicator(df["close"], window=config.rsi_period)
        rsi = _safe_float(rsi_indicator.rsi().iloc[-1])

    # Calculate Stochastic
    stoch_k, stoch_d = None, None
    if len(df) >= config.stoch_k_period:
        stoch = StochasticOscillator(
            df["high"], df["low"], df["close"],
            window=config.stoch_k_period,
            smooth_window=config.stoch_d_period,
        )
        stoch_k = _safe_float(stoch.stoch().iloc[-1])
        stoch_d = _safe_float(stoch.stoch_signal().iloc[-1])

    # Calculate MACD
    macd_val, macd_signal_val, macd_hist = None, None, None
    if len(df) >= config.macd_slow:
        macd = MACD(
            df["close"],
            window_fast=config.macd_fast,
            window_slow=config.macd_slow,
            window_sign=config.macd_signal,
        )
        macd_val = _safe_float(macd.macd().iloc[-1])
        macd_signal_val = _safe_float(macd.macd_signal().iloc[-1])
        macd_hist = _safe_float(macd.macd_diff().iloc[-1])

    # Calculate ADX
    adx_val, plus_di, minus_di = None, None, None
    if len(df) >= config.adx_period:
        adx = ADXIndicator(
            df["high"], df["low"], df["close"],
            window=config.adx_period,
        )
        adx_val = _safe_float(adx.adx().iloc[-1])
        plus_di = _safe_float(adx.adx_pos().iloc[-1])
        minus_di = _safe_float(adx.adx_neg().iloc[-1])

    # Calculate ATR
    atr = None
    if len(df) >= config.atr_period:
        atr_indicator = AverageTrueRange(
            df["high"], df["low"], df["close"],
            window=config.atr_period,
        )
        atr = _safe_float(atr_indicator.average_true_range().iloc[-1])

    # Calculate Bollinger Bands
    bb_upper, bb_middle, bb_lower, bb_percent = None, None, None, None
    if len(df) >= config.bb_period:
        bb = BollingerBands(
            df["close"],
            window=config.bb_period,
            window_dev=config.bb_std,
        )
        bb_upper = _safe_float(bb.bollinger_hband().iloc[-1])
        bb_middle = _safe_float(bb.bollinger_mavg().iloc[-1])
        bb_lower = _safe_float(bb.bollinger_lband().iloc[-1])
        bb_percent = _safe_float(bb.bollinger_pband().iloc[-1])

    # Calculate VWAP
    vwap = None
    if len(df) >= 1:
        vwap_indicator = VolumeWeightedAveragePrice(
            df["high"], df["low"], df["close"], df["volume"],
        )
        vwap = _safe_float(vwap_indicator.volume_weighted_average_price().iloc[-1])
    
    return IndicatorResult(
        symbol=symbol,
        timeframe=timeframe,
        open=float(latest["open"]),
        high=float(latest["high"]),
        low=float(latest["low"]),
        close=float(latest["close"]),
        volume=int(latest["volume"]),
        sma=sma_values,
        ema=ema_values,
        rsi=rsi,
        stoch_k=stoch_k,
        stoch_d=stoch_d,
        macd=macd_val,
        macd_signal=macd_signal_val,
        macd_histogram=macd_hist,
        adx=adx_val,
        plus_di=plus_di,
        minus_di=minus_di,
        atr=atr,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        bb_percent=bb_percent,
        vwap=vwap,
    )


class IndicatorCache:
    """
    Memoizes indicator results so they are recomputed only when the data changes.

    Keyed by (symbol, timeframe, last-bar timestamp, bar count, last close), so a hit
    requires identical inputs — when computing on settled bars the key is stable for the
    whole trading day (compute once instead of every cycle); a forming bar whose close
    moves busts the key correctly. Assumes the default IndicatorConfig.
    """

    _MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._cache: dict[tuple[Any, ...], IndicatorResult] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(df: pd.DataFrame, symbol: str, timeframe: Timeframe) -> tuple[Any, ...]:
        if len(df) == 0:
            return (symbol, timeframe.value, None, 0, None)
        last_close = _safe_float(df["close"].iloc[-1])
        return (
            symbol,
            timeframe.value,
            str(df.index[-1]),
            len(df),
            round(last_close, 6) if last_close is not None else None,
        )

    def get_or_compute(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: Timeframe = Timeframe.D1,
        config: IndicatorConfig | None = None,
    ) -> IndicatorResult:
        key = self._key(df, symbol, timeframe)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached

        self.misses += 1
        result = calculate_indicators(df, symbol, timeframe, config)
        self._cache[key] = result
        if len(self._cache) > self._MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        return result

    def clear(self) -> None:
        self._cache.clear()
        self.hits = 0
        self.misses = 0


_indicator_cache: IndicatorCache | None = None


def get_indicator_cache() -> IndicatorCache:
    """Get or create the shared IndicatorCache."""
    global _indicator_cache
    if _indicator_cache is None:
        _indicator_cache = IndicatorCache()
    return _indicator_cache


def reset_indicator_cache() -> None:
    """Reset the shared IndicatorCache (test isolation)."""
    global _indicator_cache
    _indicator_cache = None


def aggregate_candles(
    ticks: list[dict],
    timeframe: Timeframe,
) -> pd.DataFrame:
    """
    Aggregate tick data into OHLCV candles.
    
    Args:
        ticks: List of tick dictionaries with 'price', 'volume', 'timestamp'
        timeframe: Target candle timeframe
        
    Returns:
        DataFrame with OHLCV columns
    """
    if not ticks:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    
    df = pd.DataFrame(ticks)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.set_index("timestamp", inplace=True)
    
    # Map timeframe to pandas resample string
    resample_map = {
        Timeframe.M1: "1min",
        Timeframe.M5: "5min",
        Timeframe.M15: "15min",
        Timeframe.M30: "30min",
        Timeframe.H1: "1h",
        Timeframe.H4: "4h",
        Timeframe.D1: "1D",
    }
    
    resampled = df.resample(resample_map[timeframe]).agg({
        "price": ["first", "max", "min", "last"],
        "volume": "sum",
    })
    
    resampled.columns = ["open", "high", "low", "close", "volume"]
    resampled.dropna(inplace=True)
    
    return resampled
