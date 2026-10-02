"""
Is there an edge? The statistics and the gate for a v2 backtest (plan M11.2; audit §Y.3).

From a :class:`~src.backtesting.session.BacktestResult`:

* **per-trade net % returns** (net P&L over the entry notional) with their mean, a seeded
  **bootstrap 95% CI** of the mean and the **t-stat**;
* **the gate:** VALIDATED only with **n ≥ 200** closed trades **and** a CI lower bound **> 0**
  - anything less is NOT VALIDATED, with the reasons;
* **benchmarks:** NIFTY and the equal-weight buy & hold of the traded symbols over the period;
* **Monte Carlo drawdown:** the trades' order reshuffled (seeded) to see how deep the drawdown
  could have been with the same trades - median and 95th percentile;
* **regime split:** the per-trade statistics by the regime label of the entry session;
* **Sortino** and the **running-peak drawdown** of the daily equity.

The gate is necessary, not sufficient: the data are current-listed names (survivorship) unless the
universe comes from a point-in-time source (M11.3), and the fills are modelled.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np

from src.domain.events import TradeClosed
from src.utils.market_time import IST

MIN_TRADES = 200
TRADING_DAYS = 252


def trade_return_pct(trade: TradeClosed) -> float:
    """Net P&L over the entry notional, in percent."""
    notional = trade.entry_price * trade.quantity
    return float(trade.net_pnl / notional * 100) if notional else 0.0


@dataclass(frozen=True)
class TradeStats:
    n: int
    mean_pct: float | None
    median_pct: float | None
    stdev_pct: float | None
    win_rate: float | None
    t_stat: float | None
    ci95: tuple[float, float] | None  # bootstrap CI of the mean per-trade return, %


def trade_stats(returns: Sequence[float], *, samples: int = 2000, seed: int = 7) -> TradeStats:
    n = len(returns)
    if n == 0:
        return TradeStats(0, None, None, None, None, None, None)
    arr = np.asarray(returns, dtype=float)
    sd = float(arr.std(ddof=1)) if n > 1 else None
    t = float(arr.mean() / (sd / math.sqrt(n))) if sd else None
    ci = None
    if n > 1:
        rng = np.random.default_rng(seed)
        means = rng.choice(arr, size=(samples, n), replace=True).mean(axis=1)
        ci = (
            round(float(np.percentile(means, 2.5)), 4),
            round(float(np.percentile(means, 97.5)), 4),
        )
    return TradeStats(
        n=n, mean_pct=round(float(arr.mean()), 4), median_pct=round(float(np.median(arr)), 4),
        stdev_pct=round(sd, 4) if sd is not None else None,
        win_rate=round(float((arr > 0).mean()), 4), t_stat=round(t, 3) if t is not None else None,
        ci95=ci,
    )  # fmt: skip


def max_drawdown_pct(equity: Sequence[float]) -> float:
    """The deepest fall from a running peak, in percent of that peak."""
    peak, worst = -math.inf, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return round(worst, 3)


def sortino(daily_returns: Sequence[float]) -> float | None:
    """Annualised mean over downside deviation (target 0)."""
    if len(daily_returns) < 2:
        return None
    arr = np.asarray(daily_returns, dtype=float)
    downside = np.sqrt(np.mean(np.minimum(arr, 0.0) ** 2))
    if downside == 0:
        return None
    return round(float(arr.mean() / downside * math.sqrt(TRADING_DAYS)), 3)


def monte_carlo_drawdown(
    returns: Sequence[float], *, weight: float, samples: int = 2000, seed: int = 7
) -> dict[str, float] | None:
    """Max drawdown of compounding the trades in random orders; ``weight`` is the fraction of
    equity each trade risks (its notional over capital)."""
    if len(returns) < 2:
        return None
    rng = np.random.default_rng(seed)
    arr = np.asarray(returns, dtype=float) / 100 * weight
    depths = []
    for _ in range(samples):
        path = np.cumprod(1 + rng.permutation(arr))
        depths.append(max_drawdown_pct([1.0, *path.tolist()]))
    return {"p50": round(float(np.percentile(depths, 50)), 3),
            "p95": round(float(np.percentile(depths, 95)), 3)}  # fmt: skip


def period_return_pct(closes: Mapping[date, float], start: date, end: date) -> float | None:
    """Close-to-close over the period: the last close before ``start`` to the last one ≤ end."""
    before = [d for d in closes if d < start]
    inside = [d for d in closes if start <= d <= end]
    if not inside:
        return None
    base = closes[max(before)] if before else closes[min(inside)]
    return round((closes[max(inside)] / base - 1) * 100, 3) if base else None


@dataclass(frozen=True)
class EdgeReport:
    start: date
    end: date
    sessions: int
    trades: TradeStats
    by_strategy: dict[str, TradeStats]
    by_regime: dict[str, TradeStats]
    portfolio_return_pct: float | None
    nifty_return_pct: float | None
    buy_hold_return_pct: float | None  # equal-weight buy & hold of the traded symbols
    sortino: float | None
    max_drawdown_pct: float
    monte_carlo_drawdown_pct: dict[str, float] | None
    verdict: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def edge_report(
    trades: Sequence[TradeClosed],
    equity: Mapping[date, Decimal],
    *,
    capital: Decimal,
    start: date,
    end: date,
    sessions: int,
    regimes: Mapping[date, str] | None = None,
    index_closes: Mapping[date, float] | None = None,
    symbol_closes: Mapping[str, Mapping[date, float]] | None = None,
    position_weight: float = 0.10,
    samples: int = 2000,
    seed: int = 7,
) -> EdgeReport:
    returns = [trade_return_pct(t) for t in trades]
    stats = trade_stats(returns, samples=samples, seed=seed)
    grouped: dict[str, list[float]] = defaultdict(list)
    by_regime: dict[str, list[float]] = defaultdict(list)
    for t, r in zip(trades, returns, strict=True):
        grouped[t.strategy].append(r)
        label = (regimes or {}).get(t.entry_ts.astimezone(IST).date(), "unknown")
        by_regime[label].append(r)
    series = [float(capital)] + [float(equity[d]) for d in sorted(equity)]
    daily = [b / a - 1 for a, b in zip(series, series[1:], strict=False) if a]
    traded = sorted({t.instrument_key for t in trades})
    holds = [held for k in traded
             if (held := period_return_pct((symbol_closes or {}).get(k, {}), start, end))
             is not None]  # fmt: skip
    reasons = []
    if stats.n < MIN_TRADES:
        reasons.append(f"{stats.n} closed trades < {MIN_TRADES}: too few to judge")
    if stats.ci95 is None or stats.ci95[0] <= 0:
        low = "n/a" if stats.ci95 is None else f"{stats.ci95[0]:.3f}%"
        reasons.append(f"95% CI lower bound of the mean net return per trade is {low}, not > 0")
    return EdgeReport(
        start=start, end=end, sessions=sessions, trades=stats,
        by_strategy={k: trade_stats(v, samples=samples, seed=seed) for k, v in sorted(grouped.items())},
        by_regime={k: trade_stats(v, samples=samples, seed=seed) for k, v in sorted(by_regime.items())},
        portfolio_return_pct=round((series[-1] / series[0] - 1) * 100, 3) if len(series) > 1 else None,
        nifty_return_pct=period_return_pct(index_closes or {}, start, end),
        buy_hold_return_pct=round(float(np.mean(holds)), 3) if holds else None,
        sortino=sortino(daily), max_drawdown_pct=max_drawdown_pct(series),
        monte_carlo_drawdown_pct=monte_carlo_drawdown(returns, weight=position_weight,
                                                      samples=samples, seed=seed),
        verdict="VALIDATED" if not reasons else "NOT VALIDATED", reasons=reasons,
    )  # fmt: skip
