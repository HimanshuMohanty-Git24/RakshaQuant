"""
Strategy Performance Tracker

Tracks real win/loss rates per strategy-regime combination from actual trade history.
Replaces hardcoded performance data in strategy_selection.py with real metrics.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default persistence location for the singleton tracker.
DEFAULT_HISTORY_FILE = Path("performance_history.json")


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
            "strategy": self.strategy,
            "regime": self.regime,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "win_rate": self.win_rate,
            "total_pnl": self.total_pnl,
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
        "trending_up": {
            "momentum": 0.60,
            "trend_following": 0.58,
            "breakout": 0.45,
            "mean_reversion": 0.40,
        },
        "trending_down": {
            "momentum": 0.55,
            "trend_following": 0.52,
            "breakout": 0.40,
            "mean_reversion": 0.42,
        },
        "ranging": {
            "mean_reversion": 0.58,
            "breakout": 0.48,
            "momentum": 0.38,
            "trend_following": 0.35,
        },
        "volatile": {
            "breakout": 0.45,
            "momentum": 0.40,
            "mean_reversion": 0.38,
            "trend_following": 0.35,
        },
    }

    def __init__(self, min_trades_for_real_data: int = 5, state_file: Path | None = None):
        self.min_trades = min_trades_for_real_data
        self.state_file = state_file
        self._records: list[dict[str, Any]] = []
        self._performance_cache: dict[tuple[str, str], StrategyPerformance] = {}
        self._load()

    def _load(self) -> None:
        """Load persisted trade history so win-rate stats survive restarts."""
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            for record in data:
                record["timestamp"] = datetime.fromisoformat(record["timestamp"])
                self._records.append(record)
            logger.info(
                "Loaded %d performance records from %s", len(self._records), self.state_file
            )
        except Exception as e:
            logger.warning("Could not load performance history (%s); starting empty.", e)

    def _save(self) -> None:
        """Persist trade history atomically (temp file + os.replace)."""
        if self.state_file is None:
            return
        try:
            serializable = [{**r, "timestamp": r["timestamp"].isoformat()} for r in self._records]
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(serializable, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning("Could not persist performance history (%s).", e)

    def record_trade(
        self,
        strategy: str,
        regime: str,
        pnl: float,
        pnl_pct: float,
        symbol: str = "",
        timestamp: datetime | None = None,
    ):
        """Record a completed trade outcome."""
        self._records.append(
            {
                "strategy": strategy,
                "regime": regime,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "symbol": symbol,
                "is_winner": pnl > 0,
                "timestamp": timestamp or datetime.now(),
            }
        )
        # Invalidate cache for this pair
        self._performance_cache.pop((strategy, regime), None)
        self._save()
        logger.debug(f"Recorded trade: {strategy}/{regime} PnL={pnl:+.2f}")

    def get_strategy_performance(
        self, strategy: str, regime: str, lookback_days: int = 30
    ) -> StrategyPerformance:
        """Get performance for a specific strategy-regime combination."""
        cache_key = (strategy, regime)
        if cache_key in self._performance_cache:
            return self._performance_cache[cache_key]

        cutoff = datetime.now() - timedelta(days=lookback_days)
        trades = [
            r
            for r in self._records
            if r["strategy"] == strategy and r["regime"] == regime and r["timestamp"] >= cutoff
        ]

        if len(trades) >= self.min_trades:
            winners = sum(1 for t in trades if t["is_winner"])
            perf = StrategyPerformance(
                strategy=strategy,
                regime=regime,
                total_trades=len(trades),
                winning_trades=winners,
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
                strategy=strategy,
                regime=regime,
                total_trades=len(trades),
                winning_trades=sum(1 for t in trades if t["is_winner"]),
                losing_trades=sum(1 for t in trades if not t["is_winner"]),
                win_rate=blended_rate,
                total_pnl=sum(t["pnl"] for t in trades) if trades else 0,
                avg_pnl_pct=sum(t["pnl_pct"] for t in trades) / len(trades) if trades else 0,
            )

        self._performance_cache[cache_key] = perf
        return perf

    def get_all_strategy_performance(
        self, regime: str, lookback_days: int = 30
    ) -> dict[str, float]:
        """Get win rates for all strategies in a given regime (for strategy selection agent)."""
        strategies = ["momentum", "mean_reversion", "breakout", "trend_following"]
        return {
            s: self.get_strategy_performance(s, regime, lookback_days).win_rate for s in strategies
        }

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

        by_strategy: dict[str, dict[str, Any]] = {}
        for record in self._records:
            bucket = by_strategy.setdefault(
                record["strategy"], {"trades": 0, "winners": 0, "total_pnl": 0.0}
            )
            bucket["trades"] += 1
            bucket["winners"] += 1 if record["is_winner"] else 0
            bucket["total_pnl"] += record["pnl"]
        for bucket in by_strategy.values():
            bucket["win_rate"] = bucket["winners"] / bucket["trades"] if bucket["trades"] else 0.0

        return {
            "total_trades": len(self._records),
            "overall_win_rate": winners / len(self._records),
            "total_pnl": sum(r["pnl"] for r in self._records),
            "by_strategy": by_strategy,
        }


# Module-level singleton
_tracker_instance: PerformanceTracker | None = None


def get_performance_tracker() -> PerformanceTracker:
    """Get the global performance tracker singleton (persisted to DEFAULT_HISTORY_FILE)."""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = PerformanceTracker(state_file=DEFAULT_HISTORY_FILE)
    return _tracker_instance
