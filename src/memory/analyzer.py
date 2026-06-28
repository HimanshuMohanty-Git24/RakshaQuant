"""
Trade Outcome Analyzer Module

Analyzes trade outcomes to compute performance metrics like MAE/MFE,
win/loss rates, and identifies patterns for learning.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.execution.journal import TradeJournal

logger = logging.getLogger(__name__)


@dataclass
class TradeOutcome:
    """Analyzed outcome of a trade."""
    
    trade_id: str
    symbol: str
    strategy: str
    regime: str
    
    # Result
    is_winner: bool
    profit_loss: float
    profit_loss_pct: float
    
    # Execution quality
    mae: float  # Maximum Adverse Excursion (worst drawdown during trade)
    mfe: float  # Maximum Favorable Excursion (best profit during trade)
    efficiency: float  # How much of MFE was captured (exit_profit / mfe)
    
    # Timing
    hold_duration_minutes: int
    was_premature_exit: bool  # Exited before reaching target
    was_late_exit: bool  # Let winner turn into loser
    hit_stop_loss: bool
    hit_target: bool
    
    # Context
    entry_conditions: dict[str, Any] = field(default_factory=dict)
    exit_conditions: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "regime": self.regime,
            "is_winner": self.is_winner,
            "profit_loss": self.profit_loss,
            "profit_loss_pct": self.profit_loss_pct,
            "mae": self.mae,
            "mfe": self.mfe,
            "efficiency": self.efficiency,
            "hold_duration_minutes": self.hold_duration_minutes,
            "was_premature_exit": self.was_premature_exit,
            "was_late_exit": self.was_late_exit,
            "hit_stop_loss": self.hit_stop_loss,
            "hit_target": self.hit_target,
            "entry_conditions": self.entry_conditions,
            "exit_conditions": self.exit_conditions,
        }


def compute_outcome(trade: dict[str, Any]) -> TradeOutcome | None:
    """
    Compute a detailed TradeOutcome from a trade dict.

    Standalone (no journal/DB needed) so the live loop can turn a just-closed position into
    a TradeOutcome directly for the learning feedback loop.
    """
    try:
        entry_price = trade.get("entry_price", 0)
        exit_price = trade.get("exit_price", 0)
        stop_loss = trade.get("stop_loss", 0)
        target = trade.get("target_price", 0)

        pnl = trade.get("profit_loss", 0)
        pnl_pct = trade.get("profit_loss_pct", 0)
        mae = trade.get("mae", 0)
        mfe = trade.get("mfe", 0)

        is_winner = pnl > 0

        # Calculate efficiency (how much of max favorable move was captured)
        if mfe > 0:
            efficiency = pnl / mfe if pnl > 0 else 0
        else:
            efficiency = 0

        # Determine exit type
        is_buy = trade.get("side") == "BUY"

        if is_buy:
            hit_stop = exit_price <= stop_loss if stop_loss else False
            hit_target = exit_price >= target if target else False
        else:
            hit_stop = exit_price >= stop_loss if stop_loss else False
            hit_target = exit_price <= target if target else False

        # Check for premature/late exit
        was_premature = mfe > 0 and pnl < mfe * 0.5 and pnl > 0  # Won but captured <50% of MFE
        was_late = mfe > abs(pnl) and pnl < 0  # Had gains but ended in loss

        return TradeOutcome(
            trade_id=trade.get("trade_id", ""),
            symbol=trade.get("symbol", ""),
            strategy=trade.get("strategy", ""),
            regime=trade.get("regime", "unknown"),
            is_winner=is_winner,
            profit_loss=pnl,
            profit_loss_pct=pnl_pct,
            mae=mae,
            mfe=mfe,
            efficiency=efficiency,
            hold_duration_minutes=trade.get("hold_duration_minutes", 0),
            was_premature_exit=was_premature,
            was_late_exit=was_late,
            hit_stop_loss=hit_stop,
            hit_target=hit_target,
        )

    except Exception as e:
        logger.error(f"Failed to compute outcome: {e}")
        return None


@dataclass
class TradeOutcomeAnalyzer:
    """
    Analyzes trade outcomes for learning and improvement.
    
    Computes performance metrics and identifies patterns
    that can be used to generate learning lessons.
    """
    
    journal: TradeJournal = None
    
    def __post_init__(self):
        if self.journal is None:
            self.journal = TradeJournal()
    
    def analyze_trade(self, trade_id: str) -> TradeOutcome | None:
        """
        Analyze a single completed trade.
        
        Args:
            trade_id: ID of the trade to analyze
            
        Returns:
            TradeOutcome with detailed analysis
        """
        trade = self.journal.get_trade(trade_id)
        
        if not trade:
            logger.warning(f"Trade not found: {trade_id}")
            return None
        
        if trade.get("status") != "closed":
            logger.warning(f"Trade not closed yet: {trade_id}")
            return None
        
        return self._compute_outcome(trade)
    
    def analyze_recent_trades(
        self,
        hours: int = 24,
        strategy: str | None = None,
    ) -> list[TradeOutcome]:
        """Analyze all trades from the last N hours."""
        
        start_date = datetime.now() - timedelta(hours=hours)
        trades = self.journal.get_trades_by_date(start_date)
        
        if strategy:
            trades = [t for t in trades if t.get("strategy") == strategy]
        
        outcomes = []
        for trade in trades:
            if trade.get("status") == "closed":
                outcome = self._compute_outcome(trade)
                if outcome:
                    outcomes.append(outcome)
        
        return outcomes
    
    def get_strategy_performance(
        self,
        strategy: str,
        days: int = 30,
    ) -> dict[str, Any]:
        """
        Get aggregated performance metrics for a strategy.
        
        Returns detailed statistics for strategy evaluation.
        """
        start_date = datetime.now() - timedelta(days=days)
        trades = self.journal.get_trades_by_date(start_date)
        trades = [t for t in trades if t.get("strategy") == strategy and t.get("status") == "closed"]
        
        if not trades:
            return {"strategy": strategy, "message": "No trades found"}
        
        outcomes = [self._compute_outcome(t) for t in trades]
        outcomes = [o for o in outcomes if o is not None]
        
        winners = [o for o in outcomes if o.is_winner]
        losers = [o for o in outcomes if not o.is_winner]
        
        return {
            "strategy": strategy,
            "total_trades": len(outcomes),
            "win_rate": len(winners) / len(outcomes) * 100,
            "avg_profit_pct": sum(o.profit_loss_pct for o in outcomes) / len(outcomes),
            "avg_winner_pct": sum(o.profit_loss_pct for o in winners) / len(winners) if winners else 0,
            "avg_loser_pct": sum(o.profit_loss_pct for o in losers) / len(losers) if losers else 0,
            "avg_efficiency": sum(o.efficiency for o in outcomes) / len(outcomes),
            "premature_exits_pct": sum(1 for o in outcomes if o.was_premature_exit) / len(outcomes) * 100,
            "late_exits_pct": sum(1 for o in outcomes if o.was_late_exit) / len(outcomes) * 100,
            "stop_loss_hit_pct": sum(1 for o in outcomes if o.hit_stop_loss) / len(outcomes) * 100,
            "target_hit_pct": sum(1 for o in outcomes if o.hit_target) / len(outcomes) * 100,
            "avg_hold_duration_minutes": sum(o.hold_duration_minutes for o in outcomes) / len(outcomes),
            "by_regime": self._group_by_regime(outcomes),
        }
    
    def identify_patterns(
        self,
        outcomes: list[TradeOutcome],
    ) -> list[dict[str, Any]]:
        """
        Identify patterns in trade outcomes.
        
        Returns patterns that could become learning lessons.
        """
        patterns = []
        
        if not outcomes:
            return patterns
        
        # Pattern 1: Strategy-regime mismatch
        regime_performance = self._group_by_regime(outcomes)
        for regime, stats in regime_performance.items():
            if stats["count"] >= 3 and stats["win_rate"] < 40:
                patterns.append({
                    "type": "regime_mismatch",
                    "severity": "high",
                    "description": f"Poor performance in {regime} regime (win rate: {stats['win_rate']:.0f}%)",
                    "context": {"regime": regime, "stats": stats},
                })
        
        # Pattern 2: Overtrading (too many trades with low win rate)
        if len(outcomes) > 10:
            recent = outcomes[:10]
            win_rate = sum(1 for o in recent if o.is_winner) / len(recent) * 100
            if win_rate < 40:
                patterns.append({
                    "type": "overtrading",
                    "severity": "medium",
                    "description": f"Recent 10 trades have {win_rate:.0f}% win rate - consider reducing frequency",
                    "context": {"recent_trades": len(recent), "win_rate": win_rate},
                })
        
        # Pattern 3: Poor timing (many premature exits)
        premature_rate = sum(1 for o in outcomes if o.was_premature_exit) / len(outcomes) * 100
        if premature_rate > 30:
            patterns.append({
                "type": "poor_timing",
                "severity": "medium",
                "description": f"{premature_rate:.0f}% of trades exited prematurely - targets may be too aggressive",
                "context": {"premature_rate": premature_rate},
            })
        
        # Pattern 4: Stop loss placement issues
        avg_mae = sum(o.mae for o in outcomes) / len(outcomes)
        stop_loss_rate = sum(1 for o in outcomes if o.hit_stop_loss) / len(outcomes) * 100
        if stop_loss_rate > 50:
            patterns.append({
                "type": "stop_loss_issue",
                "severity": "high",
                "description": f"Stop loss triggered in {stop_loss_rate:.0f}% of trades - stops may be too tight",
                "context": {"stop_loss_rate": stop_loss_rate, "avg_mae": avg_mae},
            })
        
        # Pattern 5: Low efficiency (not capturing gains)
        avg_efficiency = sum(o.efficiency for o in outcomes if o.is_winner) / len([o for o in outcomes if o.is_winner]) if any(o.is_winner for o in outcomes) else 0
        if avg_efficiency < 0.5 and any(o.is_winner for o in outcomes):
            patterns.append({
                "type": "low_efficiency",
                "severity": "medium",
                "description": f"Average efficiency is {avg_efficiency:.0%} - capturing less than half of potential gains",
                "context": {"avg_efficiency": avg_efficiency},
            })
        
        return patterns
    
    def _compute_outcome(self, trade: dict[str, Any]) -> TradeOutcome | None:
        """Compute detailed outcome for a trade (delegates to the module-level helper)."""
        return compute_outcome(trade)

    def _group_by_regime(
        self,
        outcomes: list[TradeOutcome],
    ) -> dict[str, dict[str, Any]]:
        """Group outcomes by market regime."""
        by_regime = {}
        
        for outcome in outcomes:
            regime = outcome.regime
            if regime not in by_regime:
                by_regime[regime] = {"trades": [], "count": 0, "win_rate": 0}
            
            by_regime[regime]["trades"].append(outcome)
            by_regime[regime]["count"] += 1
        
        # Calculate stats per regime
        for regime, data in by_regime.items():
            winners = sum(1 for o in data["trades"] if o.is_winner)
            data["win_rate"] = winners / data["count"] * 100 if data["count"] > 0 else 0
            data["avg_pnl"] = sum(o.profit_loss for o in data["trades"]) / data["count"] if data["count"] > 0 else 0
            del data["trades"]  # Remove raw data
        
        return by_regime
