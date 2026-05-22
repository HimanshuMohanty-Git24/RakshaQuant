"""
Strategy Performance Tracker

Tracks real win/loss rates per strategy-regime combination from actual trade history.
Replaces hardcoded performance data in strategy_selection.py with real metrics.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StrategyPerformance:
    """Performance metrics for a strategy-regime pair."""
    strategy: str
    regime: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    avg_pnl_pct: float = 0.0
    win_rate: float = 0.5  # Default to 50% when no data

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy, "regime": self.regime,
            "total_trades": self.total_trades, "winning_trades": self.winning_trades,
            "win_rate": self.win_rate, "total_pnl": self.total_pnl,
            "avg_pnl_pct": self.avg_pnl_pct,
        }


class PerformanceTracker:
    """
    Tracks and retrieves real strategy performance data.

    Records trade outcomes indexed by (strategy, regime) and provides
    rolling performance metrics for strategy selection decisions.
    """

    # Default performance priors (used when no real data available)
    DEFAULT_PERFORMANCE = {
        "trending_up": {"momentum": 0.60, "trend_following": 0.58, "breakout": 0.45, "mean_reversion": 0.40},
        "trending_down": {"momentum": 0.55, "trend_following": 0.52, "breakout": 0.40, "mean_reversion": 0.42},
        "ranging": {"mean_reversion": 0.58, "breakout": 0.48, "momentum": 0.38, "trend_following": 0.35},
        "volatile": {"breakout": 0.45, "momentum": 0.40, "mean_reversion": 0.38, "trend_following": 0.35},
    }

    def __init__(self, min_trades_for_real_data: int = 5):
        self.min_trades = min_trades_for_real_data
        self._records: list[dict[str, Any]] = []
        self._performance_cache: dict[tuple[str, str], StrategyPerformance] = {}

    def record_trade(self, strategy: str, regime: str, pnl: float, pnl_pct: float,
                     symbol: str = "", timestamp: datetime | None = None):
        """Record a completed trade outcome."""
        self._records.append({
            "strategy": strategy, "regime": regime, "pnl": pnl,
            "pnl_pct": pnl_pct, "symbol": symbol, "is_winner": pnl > 0,
            "timestamp": timestamp or datetime.now(),
        })
        # Invalidate cache for this pair
        self._performance_cache.pop((strategy, regime), None)
        logger.debug(f"Recorded trade: {strategy}/{regime} PnL={pnl:+.2f}")

    def get_strategy_performance(self, strategy: str, regime: str,
                                  lookback_days: int = 30) -> StrategyPerformance:
        """Get performance for a specific strategy-regime combination."""
        cache_key = (strategy, regime)
        if cache_key in self._performance_cache:
            return self._performance_cache[cache_key]

        cutoff = datetime.now() - timedelta(days=lookback_days)
        trades = [r for r in self._records
                  if r["strategy"] == strategy and r["regime"] == regime
                  and r["timestamp"] >= cutoff]

        if len(trades) >= self.min_trades:
            winners = sum(1 for t in trades if t["is_winner"])
            perf = StrategyPerformance(
                strategy=strategy, regime=regime,
                total_trades=len(trades), winning_trades=winners,
                losing_trades=len(trades) - winners,
                win_rate=winners / len(trades),
                total_pnl=sum(t["pnl"] for t in trades),
                avg_pnl_pct=sum(t["pnl_pct"] for t in trades) / len(trades),
            )
        else:
            # Use default prior with blend if partial data exists
            default_rate = self.DEFAULT_PERFORMANCE.get(regime, {}).get(strategy, 0.45)
            if trades:
                real_rate = sum(1 for t in trades if t["is_winner"]) / len(trades)
                blend = len(trades) / self.min_trades
                blended_rate = real_rate * blend + default_rate * (1 - blend)
            else:
                blended_rate = default_rate
            perf = StrategyPerformance(
                strategy=strategy, regime=regime,
                total_trades=len(trades), winning_trades=sum(1 for t in trades if t["is_winner"]),
                losing_trades=sum(1 for t in trades if not t["is_winner"]),
                win_rate=blended_rate,
                total_pnl=sum(t["pnl"] for t in trades) if trades else 0,
                avg_pnl_pct=sum(t["pnl_pct"] for t in trades) / len(trades) if trades else 0,
            )

        self._performance_cache[cache_key] = perf
        return perf

    def get_all_strategy_performance(self, regime: str,
                                      lookback_days: int = 30) -> dict[str, float]:
        """Get win rates for all strategies in a given regime (for strategy selection agent)."""
        strategies = ["momentum", "mean_reversion", "breakout", "trend_following"]
        return {s: self.get_strategy_performance(s, regime, lookback_days).win_rate
                for s in strategies}

    def get_best_strategies(self, regime: str, top_n: int = 2) -> list[str]:
        """Get the top N strategies for a regime by win rate."""
        rates = self.get_all_strategy_performance(regime)
        sorted_strategies = sorted(rates.items(), key=lambda x: x[1], reverse=True)
        return [s for s, _ in sorted_strategies[:top_n]]

    def get_summary(self) -> dict[str, Any]:
        """Get overall performance summary."""
        if not self._records:
            return {"total_trades": 0, "message": "No trades recorded yet"}
        winners = sum(1 for r in self._records if r["is_winner"])
        return {
            "total_trades": len(self._records),
            "overall_win_rate": winners / len(self._records),
            "total_pnl": sum(r["pnl"] for r in self._records),
            "by_strategy": {s: self.get_all_strategy_performance(r)
                           for s in set(r["strategy"] for r in self._records)
                           for r in [self._records[0]]},
        }


# Module-level singleton
_tracker_instance: PerformanceTracker | None = None


def get_performance_tracker() -> PerformanceTracker:
    """Get the global performance tracker singleton."""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = PerformanceTracker()
    return _tracker_instance
