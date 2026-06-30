"""
Tests for the walk-forward / OOS validation harness and the realistic-cost integration in
the backtest engine.
"""

import numpy as np
import pandas as pd

from src.backtesting.engine import BacktestEngine, Strategy
from src.backtesting.walk_forward import (
    MIN_OOS_TRADES,
    _metrics_from_pnls,
    aggregate_reports,
    edge_verdict,
    generate_test_folds,
    run_walk_forward,
)
from src.execution.costs import CostModel


def _uptrend(n=240):
    closes = np.linspace(100, 200, n)
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
         "Close": closes, "Volume": 1000},
        index=pd.date_range("2023-01-01", periods=n, freq="D"),
    )


class _PingPong(Strategy):
    """Deterministic: alternate BUY/SELL so trades land predictably for attribution."""

    name = "PingPong"

    def on_bar(self, row, history):
        return "BUY" if len(history) % 2 == 0 else "SELL"


# ---------------------------------------------------------------------------
# Fold generation + metrics
# ---------------------------------------------------------------------------

def test_generate_test_folds():
    folds = generate_test_folds(n_bars=200, warmup_bars=120, test_bars=40)
    assert folds == [(120, 160), (160, 200)]


def test_generate_test_folds_none_when_too_short():
    assert generate_test_folds(n_bars=100, warmup_bars=120, test_bars=40) == []


def test_metrics_from_pnls():
    m = _metrics_from_pnls([10.0, -5.0, 20.0, -5.0], initial_capital=1000.0)
    assert m["trades"] == 4
    assert m["expectancy"] == 5.0  # (10-5+20-5)/4
    assert m["win_rate"] == 50.0
    assert m["profit_factor"] == 3.0  # 30 / 10
    assert m["return_pct"] == 2.0  # 20/1000


# ---------------------------------------------------------------------------
# run_walk_forward
# ---------------------------------------------------------------------------

def test_run_walk_forward_produces_oos_folds():
    data = _uptrend(240)
    report = run_walk_forward(
        data, _PingPong, symbol="X", warmup_bars=120, test_bars=40, cost_model=CostModel.zero()
    )
    assert report.symbol == "X"
    assert report.oos_trades > 0
    assert len(report.folds) == 3  # (120,160),(160,200),(200,240)
    # On a clean uptrend, ping-pong long trades are net positive.
    assert report.oos_return_pct > 0


def test_run_walk_forward_insufficient_data():
    report = run_walk_forward(_uptrend(80), _PingPong, warmup_bars=120, test_bars=40)
    assert report.oos_trades == 0
    assert report.folds == []


# ---------------------------------------------------------------------------
# Cost integration in the engine
# ---------------------------------------------------------------------------

def test_costs_reduce_backtest_returns():
    data = _uptrend(240)
    free = BacktestEngine(cost_model=CostModel.zero()).run(_PingPong(), data, "X")
    costed = BacktestEngine(cost_model=CostModel(slippage_bps=50, brokerage_bps=10)).run(
        _PingPong(), data, "X"
    )
    assert costed.final_capital < free.final_capital  # realistic costs eat into P&L


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------

def test_edge_verdict_validated():
    v = edge_verdict(oos_trades=100, oos_expectancy=1.5, oos_return_pct=8.0, fold_consistency=0.7)
    assert v["validated"] is True
    assert v["verdict"] == "VALIDATED"


def test_edge_verdict_rejects_negative_expectancy():
    v = edge_verdict(oos_trades=100, oos_expectancy=-0.2, oos_return_pct=-3.0, fold_consistency=0.7)
    assert v["validated"] is False
    assert any("expectancy" in r for r in v["reasons"])


def test_edge_verdict_rejects_too_few_trades():
    v = edge_verdict(oos_trades=5, oos_expectancy=2.0, oos_return_pct=5.0, fold_consistency=0.9)
    assert v["validated"] is False
    assert any(str(MIN_OOS_TRADES) in r for r in v["reasons"])


def test_edge_verdict_rejects_inconsistent_edge():
    v = edge_verdict(oos_trades=100, oos_expectancy=1.0, oos_return_pct=5.0, fold_consistency=0.3)
    assert v["validated"] is False
    assert any("folds" in r for r in v["reasons"])


def test_aggregate_reports():
    data = _uptrend(240)
    r1 = run_walk_forward(data, _PingPong, symbol="A", cost_model=CostModel.zero())
    r2 = run_walk_forward(data, _PingPong, symbol="B", cost_model=CostModel.zero())
    agg = aggregate_reports([r1, r2])
    assert agg["symbols"] == 2
    assert agg["total_oos_trades"] == r1.oos_trades + r2.oos_trades
    assert "verdict" in agg
