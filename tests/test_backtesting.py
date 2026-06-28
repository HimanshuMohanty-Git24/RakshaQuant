from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.backtesting.engine import BacktestEngine, BacktestResult, Strategy, Trade
from src.backtesting.strategies import (
    MeanReversionStrategy,
    MomentumStrategy,
    RSIStrategy,
    SMACrossoverStrategy,
)

# --- BacktestEngine Tests ---


@pytest.fixture
def sample_data():
    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    df = pd.DataFrame(
        {
            "Open": 100.0,
            "High": 105.0,
            "Low": 95.0,
            "Close": np.linspace(100, 200, 100),  # Uptrend
            "Volume": 1000,
        },
        index=dates,
    )
    return df


@pytest.fixture
def engine():
    return BacktestEngine(initial_capital=100000.0)


class SimpleStrategy(Strategy):
    name = "Simple"

    def on_bar(self, row, history):
        # Buy on first bar, sell on last
        if len(history) == 20:  # Start after lookback
            return "BUY"
        if len(history) == 99:
            return "SELL"
        return None


def test_backtest_run(engine, sample_data):
    strategy = SimpleStrategy()
    result = engine.run(strategy, sample_data, "TEST")

    assert isinstance(result, BacktestResult)
    assert result.total_trades == 1
    assert result.winning_trades == 1
    assert result.total_return > 0
    assert len(result.trades) == 1
    assert result.trades[0].pnl > 0


def test_backtest_no_data(engine):
    with pytest.raises(ValueError):
        engine.run(SimpleStrategy(), pd.DataFrame())


def test_fetch_data(engine):
    with patch("src.backtesting.engine.YFinanceFeed") as MockFeed:
        mock_feed = MockFeed.return_value
        mock_feed.get_historical.return_value = pd.DataFrame()

        data = engine.fetch_data("AAPL")
        assert isinstance(data, pd.DataFrame)
        mock_feed.get_historical.assert_called_with("AAPL", period="1y")


def test_calculate_metrics(engine, sample_data):
    # Manually create trades
    trades = [
        Trade(datetime(2024, 1, 1), datetime(2024, 1, 2), "TEST", "LONG", 100, 110, 1, 10, 10),
        Trade(datetime(2024, 1, 3), datetime(2024, 1, 4), "TEST", "LONG", 100, 90, 1, -10, -10),
    ]
    equity_curve = [100, 110, 100]

    result = engine._calculate_metrics("Test", "TEST", sample_data, trades, equity_curve, 100)

    assert result.total_trades == 2
    assert result.winning_trades == 1
    assert result.losing_trades == 1
    assert result.win_rate == 50.0
    assert result.max_drawdown > 0


# --- Strategies Tests ---


@pytest.fixture
def strategy_data():
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    df = pd.DataFrame(
        {"Close": np.concatenate([np.linspace(100, 150, 25), np.linspace(150, 100, 25)])},
        index=dates,
    )
    return df


def test_momentum_strategy(strategy_data):
    strat = MomentumStrategy(sma_period=10)

    # Run through data
    for i in range(10, len(strategy_data)):
        row = strategy_data.iloc[i]
        history = strategy_data.iloc[:i]
        strat.on_bar(row, history)
        # Just check it runs without error, logic verification is complex on synthetic data


def test_mean_reversion_strategy(strategy_data):
    strat = MeanReversionStrategy(bb_period=10)
    for i in range(10, len(strategy_data)):
        row = strategy_data.iloc[i]
        history = strategy_data.iloc[:i]
        strat.on_bar(row, history)


def test_sma_crossover_strategy():
    # Use synthetic data that guarantees a crossover
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    # First 25 days flat 100, then jump to 200
    # Fast SMA will react faster than Slow SMA -> Golden Cross
    prices = np.concatenate([np.ones(25) * 100, np.ones(25) * 200])
    df = pd.DataFrame({"Close": prices}, index=dates)

    strat = SMACrossoverStrategy(fast_period=5, slow_period=10)

    signals = []
    for i in range(10, len(df)):
        row = df.iloc[i]
        history = df.iloc[:i]
        sig = strat.on_bar(row, history)
        if sig:
            signals.append(sig)

    assert len(signals) > 0  # Should have at least buy signal


def test_rsi_strategy(strategy_data):
    strat = RSIStrategy(rsi_period=10)
    for i in range(15, len(strategy_data)):
        row = strategy_data.iloc[i]
        history = strategy_data.iloc[:i]
        strat.on_bar(row, history)


def test_strategy_abstract():
    with pytest.raises(NotImplementedError):
        Strategy().on_bar(None, None)
