"""
Walk-forward / out-of-sample validation.

A single in-sample backtest proves nothing — it is trivially overfit and says nothing about
whether an edge survives out-of-sample (OOS) or across regimes. This module evaluates a
strategy on rolling OOS windows, **net of realistic costs**, and returns a blunt verdict on
whether there is a deployable edge.

Method (param-free strategies):
- Run the strategy once over the full history; indicators warm up on a leading window that is
  NOT evaluated (so there is no look-ahead — warm-up only uses past bars).
- Partition the post-warm-up region into contiguous OOS test windows ("folds").
- Aggregate trade outcomes across all OOS windows and report expectancy, win-rate, profit
  factor, return, max drawdown, and **fold consistency** (fraction of folds that were positive).

IMPORTANT — survivorship: this harness evaluates whatever symbols you pass in. If you pass
today's liquid names (or the live StockDiscovery output), results carry survivorship/selection
bias. For a production go/no-go you must supply a **point-in-time, survivorship-bias-free**
universe (including delisted/suspended names), which YFinance cannot provide — wire NSE
Bhavcopy or a vendor dataset. The verdict here is necessary but NOT sufficient.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.backtesting.engine import BacktestEngine, Strategy, Trade

# Minimum OOS trades before a verdict is statistically meaningful.
MIN_OOS_TRADES = 30


@dataclass
class FoldResult:
    fold: int
    test_start: str
    test_end: str
    trades: int
    return_pct: float
    expectancy: float


@dataclass
class WalkForwardReport:
    """OOS results for one symbol across all folds."""

    symbol: str
    oos_trades: int
    oos_return_pct: float
    oos_expectancy: float
    oos_win_rate: float
    oos_profit_factor: float
    oos_max_drawdown_pct: float
    fold_consistency: float  # fraction of folds with positive return
    folds: list[FoldResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "oos_trades": self.oos_trades,
            "oos_return_pct": round(self.oos_return_pct, 3),
            "oos_expectancy": round(self.oos_expectancy, 4),
            "oos_win_rate": round(self.oos_win_rate, 2),
            "oos_profit_factor": round(self.oos_profit_factor, 3),
            "oos_max_drawdown_pct": round(self.oos_max_drawdown_pct, 3),
            "fold_consistency": round(self.fold_consistency, 3),
            "folds": len(self.folds),
        }


def generate_test_folds(n_bars: int, warmup_bars: int, test_bars: int) -> list[tuple[int, int]]:
    """Contiguous OOS test windows (start, end) over [warmup_bars, n_bars)."""
    folds: list[tuple[int, int]] = []
    start = warmup_bars
    while start < n_bars:
        end = min(start + test_bars, n_bars)
        if end - start >= max(5, test_bars // 2):  # skip a tiny trailing remnant
            folds.append((start, end))
        start = end
    return folds


def _metrics_from_pnls(pnls: list[float], initial_capital: float) -> dict[str, float]:
    if not pnls:
        return {
            "trades": 0, "return_pct": 0.0, "expectancy": 0.0,
            "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown_pct": 0.0,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    # Equity curve (per-unit), for drawdown.
    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
    return {
        "trades": len(pnls),
        "return_pct": sum(pnls) / initial_capital * 100 if initial_capital else 0.0,
        "expectancy": sum(pnls) / len(pnls),
        "win_rate": len(wins) / len(pnls) * 100,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_pct": max_dd,
    }


def run_walk_forward(
    data: pd.DataFrame,
    strategy_factory: Callable[[], Strategy],
    *,
    symbol: str = "UNKNOWN",
    warmup_bars: int = 120,
    test_bars: int = 40,
    cost_model: Any = None,
    initial_capital: float = 100_000.0,
) -> WalkForwardReport:
    """Evaluate a strategy out-of-sample on rolling folds, net of `cost_model` costs."""
    n = len(data)
    folds = generate_test_folds(n, warmup_bars, test_bars)

    if not folds:
        return WalkForwardReport(symbol, 0, 0.0, 0.0, 0.0, 0.0, 0.0, [])

    # Run once over the full series; warm-up bars are excluded from evaluation below.
    engine = BacktestEngine(initial_capital=initial_capital, cost_model=cost_model)
    result = engine.run(strategy_factory(), data, symbol=symbol)

    # Bucket each trade into the fold its ENTRY falls in (OOS attribution).
    fold_pnls: dict[int, list[float]] = {i: [] for i in range(len(folds))}
    for trade in result.trades:
        pos = _entry_position(data, trade)
        if pos is None:
            continue
        for i, (start, end) in enumerate(folds):
            if start <= pos < end:
                fold_pnls[i].append(trade.pnl)
                break

    fold_results: list[FoldResult] = []
    all_pnls: list[float] = []
    positive_folds = 0
    evaluated_folds = 0
    for i, (start, end) in enumerate(folds):
        pnls = fold_pnls[i]
        m = _metrics_from_pnls(pnls, initial_capital)
        all_pnls.extend(pnls)
        if pnls:
            evaluated_folds += 1
            if m["return_pct"] > 0:
                positive_folds += 1
        fold_results.append(
            FoldResult(
                fold=i,
                test_start=str(data.index[start].date()),
                test_end=str(data.index[min(end, n - 1)].date()),
                trades=int(m["trades"]),
                return_pct=m["return_pct"],
                expectancy=m["expectancy"],
            )
        )

    agg = _metrics_from_pnls(all_pnls, initial_capital)
    consistency = (positive_folds / evaluated_folds) if evaluated_folds else 0.0
    return WalkForwardReport(
        symbol=symbol,
        oos_trades=int(agg["trades"]),
        oos_return_pct=agg["return_pct"],
        oos_expectancy=agg["expectancy"],
        oos_win_rate=agg["win_rate"],
        oos_profit_factor=agg["profit_factor"],
        oos_max_drawdown_pct=agg["max_drawdown_pct"],
        fold_consistency=consistency,
        folds=fold_results,
    )


def _entry_position(data: pd.DataFrame, trade: Trade) -> int | None:
    try:
        loc = data.index.get_loc(trade.entry_date)
        return int(loc) if isinstance(loc, int) else None
    except (KeyError, TypeError):
        return None


def edge_verdict(
    oos_trades: int,
    oos_expectancy: float,
    oos_return_pct: float,
    fold_consistency: float,
) -> dict[str, Any]:
    """
    Blunt go/no-go on whether there is a deployable OOS edge.

    VALIDATED requires: enough OOS trades, positive net expectancy AND return, and the edge
    showing up in a majority of folds (not one lucky window). Anything else is NOT VALIDATED.
    """
    reasons: list[str] = []
    if oos_trades < MIN_OOS_TRADES:
        reasons.append(f"only {oos_trades} OOS trades (need >= {MIN_OOS_TRADES} to be meaningful)")
    if oos_expectancy <= 0:
        reasons.append(f"net OOS expectancy is {oos_expectancy:.4f} (<= 0 after costs)")
    if oos_return_pct <= 0:
        reasons.append(f"net OOS return is {oos_return_pct:.2f}% (<= 0 after costs)")
    if fold_consistency < 0.5:
        reasons.append(
            f"edge in only {fold_consistency:.0%} of folds (< 50% — likely regime-specific/luck)"
        )
    validated = not reasons
    return {
        "verdict": "VALIDATED" if validated else "NOT VALIDATED",
        "validated": validated,
        "reasons": reasons or ["positive net-of-cost edge, consistent across folds"],
    }


def aggregate_reports(reports: list[WalkForwardReport]) -> dict[str, Any]:
    """Combine per-symbol OOS reports into a universe-level verdict."""
    total_trades = sum(r.oos_trades for r in reports)
    total_pnl_pct = sum(r.oos_return_pct for r in reports)
    # Trade-weighted expectancy across symbols.
    weighted_exp = (
        sum(r.oos_expectancy * r.oos_trades for r in reports) / total_trades
        if total_trades
        else 0.0
    )
    consistencies = [r.fold_consistency for r in reports if r.oos_trades > 0]
    avg_consistency = sum(consistencies) / len(consistencies) if consistencies else 0.0
    symbols_positive = sum(1 for r in reports if r.oos_return_pct > 0 and r.oos_trades > 0)
    evaluated = sum(1 for r in reports if r.oos_trades > 0)

    verdict = edge_verdict(total_trades, weighted_exp, total_pnl_pct, avg_consistency)
    return {
        "symbols": len(reports),
        "symbols_evaluated": evaluated,
        "symbols_positive": symbols_positive,
        "total_oos_trades": total_trades,
        "weighted_oos_expectancy": round(weighted_exp, 4),
        "summed_oos_return_pct": round(total_pnl_pct, 3),
        "avg_fold_consistency": round(avg_consistency, 3),
        **verdict,
    }
