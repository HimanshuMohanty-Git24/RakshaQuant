import numpy as np
import pandas as pd
import pytest

from src.market.indicators import (
    IndicatorConfig,
    IndicatorResult,
    Timeframe,
    aggregate_candles,
    calculate_indicators,
)
from src.market.signals import SignalEngine, SignalType, StrategyType

# --- Indicators Tests ---


@pytest.fixture
def sample_df():
    # Create sample dataframe with 100 rows
    dates = pd.date_range(start="2023-01-01", periods=100, freq="5min")
    data = {
        "open": np.linspace(100, 200, 100),
        "high": np.linspace(105, 205, 100),
        "low": np.linspace(95, 195, 100),
        "close": np.linspace(102, 202, 100),
        "volume": np.random.randint(1000, 5000, 100),
    }
    return pd.DataFrame(data, index=dates)


def test_calculate_indicators(sample_df):
    result = calculate_indicators(sample_df, "TEST")

    assert isinstance(result, IndicatorResult)
    assert result.symbol == "TEST"
    assert result.timeframe == Timeframe.M5

    # Check moving averages
    assert 20 in result.sma
    assert result.sma[20] > 0
    assert 9 in result.ema
    assert result.ema[9] > 0

    # Check momentum
    assert result.rsi is not None
    assert 0 <= result.rsi <= 100

    # Check trend
    assert result.macd is not None
    assert result.adx is not None

    # Check volatility
    assert result.atr is not None
    assert result.bb_upper is not None

    # Check VWAP
    assert result.vwap is not None


def test_calculate_indicators_config(sample_df):
    config = IndicatorConfig(sma_periods=[10], rsi_period=5)
    result = calculate_indicators(sample_df, "TEST", config=config)

    assert 10 in result.sma
    assert 20 not in result.sma  # Default not used
    assert result.rsi is not None


def test_aggregate_candles():
    ticks = [
        {"price": 100, "volume": 10, "timestamp": "2023-01-01 10:00:00"},
        {"price": 105, "volume": 20, "timestamp": "2023-01-01 10:01:00"},
        {"price": 95, "volume": 10, "timestamp": "2023-01-01 10:02:00"},
        {"price": 102, "volume": 5, "timestamp": "2023-01-01 10:04:00"},
        # Next candle
        {"price": 110, "volume": 10, "timestamp": "2023-01-01 10:06:00"},
    ]

    df = aggregate_candles(ticks, Timeframe.M5)

    assert len(df) == 2

    # First candle
    c1 = df.iloc[0]
    assert c1["open"] == 100
    assert c1["high"] == 105
    assert c1["low"] == 95
    assert c1["close"] == 102
    assert c1["volume"] == 45


# --- Signals Tests ---


@pytest.fixture
def signal_engine():
    return SignalEngine()


@pytest.fixture
def indicator_result():
    return IndicatorResult(
        symbol="TEST",
        timeframe=Timeframe.M5,
        open=100,
        high=105,
        low=95,
        close=100,
        volume=1000,
        sma={20: 95, 50: 90},
        ema={21: 95},
        rsi=40,
        stoch_k=20,
        stoch_d=20,
        macd=1,
        macd_signal=0.5,
        macd_histogram=0.5,
        adx=30,
        plus_di=25,
        minus_di=15,
        atr=2,
        bb_upper=110,
        bb_middle=100,
        bb_lower=90,
        bb_percent=0.5,
        vwap=100,
    )


def test_momentum_strategy_buy(signal_engine, indicator_result):
    # RSI < 50 and MACD hist > 0 -> BUY
    indicator_result.rsi = 45
    indicator_result.macd_histogram = 0.5

    signal = signal_engine._momentum_strategy(indicator_result)
    assert signal is not None
    assert signal.signal_type == SignalType.BUY
    assert signal.strategy == StrategyType.MOMENTUM


def test_momentum_strategy_sell(signal_engine, indicator_result):
    # RSI > 50 and MACD hist < 0 -> SELL
    indicator_result.rsi = 55
    indicator_result.macd_histogram = -0.5

    signal = signal_engine._momentum_strategy(indicator_result)
    assert signal is not None
    assert signal.signal_type == SignalType.SELL
    assert signal.strategy == StrategyType.MOMENTUM


def test_mean_reversion_strategy_buy(signal_engine, indicator_result):
    # Close <= BB lower -> BUY
    indicator_result.close = 90
    indicator_result.bb_lower = 90

    signal = signal_engine._mean_reversion_strategy(indicator_result)
    assert signal is not None
    assert signal.signal_type == SignalType.BUY
    assert signal.strategy == StrategyType.MEAN_REVERSION


def test_mean_reversion_strategy_sell(signal_engine, indicator_result):
    # Close >= BB upper -> SELL
    indicator_result.close = 110
    indicator_result.bb_upper = 110
    indicator_result.rsi = 75  # Overbought

    signal = signal_engine._mean_reversion_strategy(indicator_result)
    assert signal is not None
    assert signal.signal_type == SignalType.SELL
    assert signal.strategy == StrategyType.MEAN_REVERSION


def test_breakout_strategy(signal_engine, indicator_result):
    # Squeeze
    indicator_result.bb_upper = 101
    indicator_result.bb_lower = 99
    indicator_result.bb_middle = 100
    # width = (101-99)/100 = 0.02 < 0.1 (threshold)

    # Breakout up
    indicator_result.close = 102

    signal = signal_engine._breakout_strategy(indicator_result)
    assert signal is not None
    assert signal.signal_type == SignalType.BUY
    assert signal.strategy == StrategyType.BREAKOUT


def test_trend_following_strategy(signal_engine, indicator_result):
    # ADX > 25, +DI > -DI, Price > EMA
    indicator_result.adx = 30
    indicator_result.plus_di = 25
    indicator_result.minus_di = 15
    indicator_result.close = 100
    indicator_result.ema = {21: 95}

    signal = signal_engine._trend_following_strategy(indicator_result)
    assert signal is not None
    assert signal.signal_type == SignalType.BUY
    assert signal.strategy == StrategyType.TREND_FOLLOWING


def test_generate_signals(signal_engine, indicator_result):
    # Set up conditions for Momentum BUY
    indicator_result.rsi = 45
    indicator_result.macd_histogram = 0.5

    signals = signal_engine.generate_signals(indicator_result)

    assert len(signals) >= 1
    types = [s.strategy for s in signals]
    assert StrategyType.MOMENTUM in types


def test_signal_risk_management(signal_engine, indicator_result):
    indicator_result.close = 100
    indicator_result.atr = 2

    # Force a BUY signal via momentum
    indicator_result.rsi = 45
    indicator_result.macd_histogram = 0.5

    signal = signal_engine._momentum_strategy(indicator_result)

    # Check ATR based stops
    assert signal.stop_loss == 100 - (2 * 2)  # 96
    assert signal.target_price == 100 + (3 * 2)  # 106
    assert signal.risk_reward_ratio == 1.5  # 6/4
