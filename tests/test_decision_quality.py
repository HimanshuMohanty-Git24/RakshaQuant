"""
Tests for PR-5b/5d/5c — decision quality:

- 5b: evidence-based signal confidence (_directional_confidence) + risk-based sizing.
- 5d: RealSignalStrategy (backtester uses the live signal engine) + compare_results scorecard.
- 5c: agent prompts ask for calibration and to weigh the enrichment.
"""

import numpy as np
import pandas as pd

from src.agents.market_regime import REGIME_SYSTEM_PROMPT
from src.agents.signal_validation import VALIDATION_SYSTEM_PROMPT
from src.agents.strategy_selection import STRATEGY_SYSTEM_PROMPT
from src.backtesting.engine import BacktestResult, compare_results
from src.backtesting.strategies import RealSignalStrategy
from src.market.indicators import IndicatorResult, Timeframe
from src.market.signals import SignalEngine, SignalType
from src.market.sizing import calculate_position_size


def _ind(**over):
    base = dict(
        symbol="X",
        timeframe=Timeframe.D1,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1000,
        sma={20: 95},
        ema={21: 95},
        rsi=40,
        macd_histogram=0.5,
        plus_di=25,
        minus_di=15,
    )
    base.update(over)
    return IndicatorResult(**base)


# ---------------------------------------------------------------------------
# 5b — evidence-based confidence
# ---------------------------------------------------------------------------


def test_confidence_full_agreement_is_high():
    eng = SignalEngine()
    # All four checks confirm a BUY.
    ind = _ind(rsi=40, macd_histogram=0.5, plus_di=25, minus_di=15, close=100, ema={21: 95})
    assert eng._directional_confidence(ind, SignalType.BUY) == 0.90


def test_confidence_full_disagreement_is_low():
    eng = SignalEngine()
    ind = _ind(rsi=60, macd_histogram=-0.5, plus_di=10, minus_di=20, close=90, ema={21: 95})
    assert eng._directional_confidence(ind, SignalType.BUY) == 0.35


def test_confidence_defaults_to_mid_without_indicators():
    eng = SignalEngine()
    ind = _ind(rsi=None, macd_histogram=None, plus_di=None, minus_di=None, ema=None, sma=None)
    assert eng._directional_confidence(ind, SignalType.BUY) == 0.5


# ---------------------------------------------------------------------------
# 5b — risk-based sizing (the sizing the live loop now uses)
# ---------------------------------------------------------------------------


def test_position_size_respects_risk_and_position_caps():
    result = calculate_position_size(
        capital=100_000,
        entry_price=100,
        stop_loss=90,
        risk_per_trade=0.02,
        max_position_pct=0.10,
    )
    assert result.shares > 0
    # Never risks more than the configured per-trade budget.
    assert result.risk_percent <= 0.02 + 1e-9
    # Never exceeds the position-size cap (10% of capital).
    assert result.position_value <= 100_000 * 0.10 + 1e-6


def test_position_size_scales_with_stop_distance():
    tight = calculate_position_size(
        capital=100_000, entry_price=100, stop_loss=99, risk_per_trade=0.01, max_position_pct=1.0
    )
    wide = calculate_position_size(
        capital=100_000, entry_price=100, stop_loss=80, risk_per_trade=0.01, max_position_pct=1.0
    )
    # A wider stop (more risk per share) => fewer shares for the same risk budget.
    assert wide.shares < tight.shares


# ---------------------------------------------------------------------------
# 5d — RealSignalStrategy + scorecard
# ---------------------------------------------------------------------------


def _bars(n=60):
    closes = 100 + np.linspace(0, 6, n)
    return pd.DataFrame(
        {
            "Open": closes - 0.2,
            "High": closes + 0.5,
            "Low": closes - 0.5,
            "Close": closes,
            "Volume": np.full(n, 100_000),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    )


def test_real_signal_strategy_uses_live_engine():
    data = _bars(60)
    strat = RealSignalStrategy(symbol="X", min_bars=50)
    # Not enough history yet.
    assert strat.on_bar(data.iloc[10], data.iloc[:10]) is None
    # Enough history: returns a valid signal token (or None), never raises.
    out = strat.on_bar(data.iloc[-1], data.iloc[:-1])
    assert out in ("BUY", "SELL", None)


def _bt(**over):
    base = dict(
        strategy_name="s",
        symbol="X",
        start_date="a",
        end_date="b",
        initial_capital=100,
        final_capital=110,
        total_return=10,
        total_return_pct=10,
        total_trades=5,
        winning_trades=3,
        losing_trades=2,
        win_rate=60,
        avg_win=5,
        avg_loss=-3,
        profit_factor=1.5,
        expectancy=2,
        max_drawdown=5,
        max_drawdown_pct=5,
        sharpe_ratio=1.0,
    )
    base.update(over)
    return BacktestResult(**base)


def test_compare_results_flags_improvement():
    baseline = _bt(total_return_pct=5, expectancy=1, max_drawdown_pct=10)
    candidate = _bt(total_return_pct=8, expectancy=2, max_drawdown_pct=8)
    report = compare_results(baseline, candidate)
    assert report["improved"] is True
    assert report["deltas"]["total_return_pct"] == 3


def test_compare_results_flags_regression():
    baseline = _bt(total_return_pct=5, max_drawdown_pct=5)
    candidate = _bt(total_return_pct=3, max_drawdown_pct=9)  # worse return AND worse drawdown
    assert compare_results(baseline, candidate)["improved"] is False


# ---------------------------------------------------------------------------
# 5c — prompts ask for calibration and to weigh the enrichment
# ---------------------------------------------------------------------------


def test_regime_prompt_asks_for_calibration_and_enrichment():
    text = REGIME_SYSTEM_PROMPT.lower()
    assert "calibrate" in text
    assert "mood" in text and "prediction" in text


def test_validation_prompt_weighs_predictions_and_calibration():
    text = VALIDATION_SYSTEM_PROMPT.lower()
    assert "calibrated" in text
    assert "ml prediction" in text


def test_strategy_prompt_uses_real_win_rates():
    assert "win-rate" in STRATEGY_SYSTEM_PROMPT.lower()
