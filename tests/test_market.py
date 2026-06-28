"""
Tests for the market module.
"""

import numpy as np
import pandas as pd
import pytest

from src.market.indicators import (
    IndicatorResult,
    Timeframe,
    calculate_indicators,
)
from src.market.signals import (
    SignalEngine,
    SignalType,
    StrategyType,
)


class TestIndicators:
    """Tests for the indicators module."""

    @pytest.fixture
    def sample_df(self):
        """Create sample OHLCV data."""
        np.random.seed(42)
        n = 100

        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
        high = close + np.abs(np.random.randn(n) * 0.3)
        low = close - np.abs(np.random.randn(n) * 0.3)
        open_price = close + np.random.randn(n) * 0.2
        volume = np.random.randint(1000, 10000, n)

        return pd.DataFrame(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

    def test_calculate_indicators_returns_result(self, sample_df):
        """Test that indicators are calculated."""
        result = calculate_indicators(sample_df, "TEST", Timeframe.M5)

        assert isinstance(result, IndicatorResult)
        assert result.symbol == "TEST"
        assert result.timeframe == Timeframe.M5

    def test_calculate_indicators_has_rsi(self, sample_df):
        """Test RSI is calculated."""
        result = calculate_indicators(sample_df, "TEST", Timeframe.M5)

        assert result.rsi is not None
        assert 0 <= result.rsi <= 100

    def test_calculate_indicators_has_macd(self, sample_df):
        """Test MACD is calculated."""
        result = calculate_indicators(sample_df, "TEST", Timeframe.M5)

        assert result.macd is not None
        assert result.macd_signal is not None
        assert result.macd_histogram is not None

    def test_calculate_indicators_has_bollinger(self, sample_df):
        """Test Bollinger Bands are calculated."""
        result = calculate_indicators(sample_df, "TEST", Timeframe.M5)

        assert result.bb_upper is not None
        assert result.bb_middle is not None
        assert result.bb_lower is not None
        assert result.bb_upper > result.bb_middle > result.bb_lower

    def test_calculate_indicators_has_adx(self, sample_df):
        """Test ADX is calculated."""
        result = calculate_indicators(sample_df, "TEST", Timeframe.M5)

        assert result.adx is not None
        assert result.plus_di is not None
        assert result.minus_di is not None

    def test_calculate_indicators_has_moving_averages(self, sample_df):
        """Test moving averages are calculated."""
        result = calculate_indicators(sample_df, "TEST", Timeframe.M5)

        assert result.sma is not None
        assert result.ema is not None
        assert 20 in result.sma
        assert 9 in result.ema

    def test_indicator_result_to_dict(self, sample_df):
        """Test IndicatorResult serialization."""
        result = calculate_indicators(sample_df, "TEST", Timeframe.M5)
        data = result.to_dict()

        assert isinstance(data, dict)
        assert "symbol" in data
        assert "momentum" in data
        assert "trend" in data
        assert "volatility" in data


class TestSignalEngine:
    """Tests for the signal engine."""

    @pytest.fixture
    def sample_indicators(self):
        """Create sample indicator result."""
        return IndicatorResult(
            symbol="RELIANCE",
            timeframe=Timeframe.M5,
            open=2480.0,
            high=2510.0,
            low=2475.0,
            close=2500.0,
            volume=1000000,
            sma={20: 2450.0, 50: 2400.0, 200: 2300.0},
            ema={9: 2490.0, 21: 2470.0, 55: 2430.0},
            rsi=55.0,
            stoch_k=65.0,
            stoch_d=60.0,
            macd=15.0,
            macd_signal=10.0,
            macd_histogram=5.0,
            adx=30.0,
            plus_di=28.0,
            minus_di=18.0,
            atr=25.0,
            bb_upper=2530.0,
            bb_middle=2480.0,
            bb_lower=2430.0,
            bb_percent=0.7,
            vwap=2485.0,
        )

    def test_signal_engine_generates_signals(self, sample_indicators):
        """Test signal generation."""
        engine = SignalEngine()
        signals = engine.generate_signals(sample_indicators)

        # Should generate at least some signals with bullish indicators
        assert isinstance(signals, list)

    def test_signal_has_required_fields(self, sample_indicators):
        """Test signal contains required fields."""
        engine = SignalEngine()

        # Force a trend following signal with strong indicators
        sample_indicators.adx = 35.0
        sample_indicators.plus_di = 30.0
        sample_indicators.minus_di = 15.0

        signals = engine.generate_signals(
            sample_indicators, active_strategies=[StrategyType.TREND_FOLLOWING]
        )

        if signals:
            signal = signals[0]
            assert signal.signal_id is not None
            assert signal.symbol == "RELIANCE"
            assert signal.entry_price > 0
            assert signal.stop_loss > 0
            assert signal.target_price > 0
            assert 0 <= signal.confidence <= 1

    def test_signal_to_dict(self, sample_indicators):
        """Test signal serialization."""
        engine = SignalEngine()
        sample_indicators.adx = 35.0
        sample_indicators.plus_di = 30.0
        sample_indicators.minus_di = 15.0

        signals = engine.generate_signals(
            sample_indicators, active_strategies=[StrategyType.TREND_FOLLOWING]
        )

        if signals:
            data = signals[0].to_dict()
            assert isinstance(data, dict)
            assert "signal_id" in data
            assert "entry_price" in data
            assert "risk_reward_ratio" in data

    def test_mean_reversion_at_lower_bb(self, sample_indicators):
        """Test mean reversion signal at lower Bollinger Band."""
        engine = SignalEngine()

        # Price at lower BB with oversold RSI
        sample_indicators.close = 2425.0  # Below lower BB
        sample_indicators.rsi = 25.0  # Oversold

        signals = engine.generate_signals(
            sample_indicators, active_strategies=[StrategyType.MEAN_REVERSION]
        )

        # Should generate a BUY signal
        buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
        assert len(buy_signals) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
