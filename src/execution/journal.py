"""
Trade Journal Module

Stores trade decisions, executions, and outcomes for analysis and replay.
Uses PostgreSQL for persistence with SQLAlchemy.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine, JSON
from sqlalchemy.orm import declarative_base, sessionmaker

from src.config import get_settings

logger = logging.getLogger(__name__)

Base = declarative_base()


class TradeRecord(Base):
    """SQLAlchemy model for trade records."""
    
    __tablename__ = "trade_journal"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    
    # Trade identification
    trade_id = Column(String(50), unique=True, index=True)
    signal_id = Column(String(50), index=True)
    workflow_id = Column(String(50), index=True)
    
    # Symbol and direction
    symbol = Column(String(20), index=True)
    exchange = Column(String(10))
    side = Column(String(10))  # BUY/SELL
    strategy = Column(String(50), index=True)
    
    # Entry details
    entry_price = Column(Float)
    entry_quantity = Column(Integer)
    entry_time = Column(DateTime, index=True)
    
    # Exit details (filled after trade closes)
    exit_price = Column(Float, nullable=True)
    exit_quantity = Column(Integer, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    exit_reason = Column(String(50), nullable=True)  # target, stop_loss, manual, etc.
    
    # Levels
    stop_loss = Column(Float)
    target_price = Column(Float)
    
    # Performance (filled after trade closes)
    profit_loss = Column(Float, nullable=True)
    profit_loss_pct = Column(Float, nullable=True)
    mae = Column(Float, nullable=True)  # Maximum Adverse Excursion
    mfe = Column(Float, nullable=True)  # Maximum Favorable Excursion
    hold_duration_minutes = Column(Integer, nullable=True)
    
    # Agent decisions (JSON)
    regime = Column(String(20))
    regime_confidence = Column(Float)
    signal_confidence = Column(Float)
    validation_confidence = Column(Float)
    risk_warnings = Column(Text, nullable=True)  # JSON array
    
    # Full decision chain (JSON)
    decision_chain = Column(Text)  # Full agent reasoning
    
    # Status
    status = Column(String(20), index=True)  # open, closed, cancelled
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DecisionLog(Base):
    """SQLAlchemy model for agent decision logs."""
    
    __tablename__ = "decision_logs"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    
    workflow_id = Column(String(50), index=True)
    agent = Column(String(50), index=True)  # regime, strategy, signal, risk
    
    # Input/Output
    input_data = Column(Text)  # JSON
    output_data = Column(Text)  # JSON
    
    # Decision details
    decision = Column(String(20))
    confidence = Column(Float)
    reasoning = Column(Text)
    
    # Performance
    latency_ms = Column(Integer)
    tokens_used = Column(Integer, nullable=True)
    
    # Timestamp
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)


@dataclass
class TradeJournal:
    """
    Trade journal for recording and retrieving trade data.
    
    Provides methods for:
    - Recording new trades
    - Updating trade outcomes
    - Querying historical trades
    - Generating trade analytics
    """
    
    database_url: str = ""
    is_persistent: bool = field(default=True, init=False)
    _engine: Any = field(default=None, repr=False)
    _session: Any = field(default=None, repr=False)
    
    def __post_init__(self):
        """Initialize database connection."""
        if not self.database_url:
            settings = get_settings()
            self.database_url = settings.database_url
        
        self._initialize_db()
    
    def _initialize_db(self) -> None:
        """Initialize database and create tables."""
        try:
            self._engine = create_engine(self.database_url)
            Base.metadata.create_all(self._engine)
            Session = sessionmaker(bind=self._engine)
            self._session = Session()
            self.is_persistent = "memory" not in self.database_url
            logger.info("Trade journal database initialized")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            # Fall back to in-memory SQLite so the system keeps running, but make it LOUD:
            # the trade journal will NOT persist across restarts in this mode.
            self._engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(self._engine)
            Session = sessionmaker(bind=self._engine)
            self._session = Session()
            self.is_persistent = False
            logger.error(
                "Trade journal is using a NON-PERSISTENT in-memory database — trade history "
                "will be lost on restart. Set a reachable DATABASE_URL for durable journaling."
            )
    
    def record_trade(
        self,
        trade: dict[str, Any],
        workflow_id: str,
        state: dict[str, Any],
    ) -> str:
        """
        Record a new trade entry.
        
        Args:
            trade: Trade details from risk agent
            workflow_id: Current workflow ID
            state: Full trading state for decision chain
            
        Returns:
            Trade ID
        """
        trade_id = f"TRD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}"
        
        record = TradeRecord(
            trade_id=trade_id,
            signal_id=trade.get("signal_id", ""),
            workflow_id=workflow_id,
            symbol=trade.get("symbol", ""),
            exchange=trade.get("exchange", "NSE"),
            side=trade.get("signal_type", "BUY"),
            strategy=trade.get("strategy", ""),
            entry_price=trade.get("entry_price", 0),
            entry_quantity=trade.get("quantity", 0),
            entry_time=datetime.now(),
            stop_loss=trade.get("stop_loss", 0),
            target_price=trade.get("target_price", 0),
            regime=state.get("regime", "unknown"),
            regime_confidence=state.get("regime_confidence", 0),
            signal_confidence=trade.get("confidence", 0),
            validation_confidence=trade.get("validation", {}).get("confidence", 0),
            risk_warnings=json.dumps(state.get("risk_warnings", [])),
            decision_chain=json.dumps(self._extract_decision_chain(state)),
            status="open",
        )
        
        try:
            self._session.add(record)
            self._session.commit()
            logger.info(f"Recorded trade: {trade_id}")
        except Exception as e:
            logger.error(f"Failed to record trade: {e}")
            self._session.rollback()
        
        return trade_id
    
    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
        mae: float = 0,
        mfe: float = 0,
        pnl: float | None = None,
        exit_quantity: int | None = None,
    ) -> bool:
        """
        Close an open trade with exit details.
        
        Args:
            trade_id: Trade to close
            exit_price: Exit price
            exit_reason: Reason for exit (target, stop_loss, manual)
            mae: Maximum adverse excursion
            mfe: Maximum favorable excursion
            
        Returns:
            True if successful
        """
        try:
            record = self._session.query(TradeRecord).filter_by(trade_id=trade_id).first()
            
            if not record:
                logger.warning(f"Trade not found: {trade_id}")
                return False
            
            qty = exit_quantity if exit_quantity is not None else record.entry_quantity

            # Prefer the caller's realized P&L (which is net of fees/slippage from the
            # execution engine); otherwise fall back to a gross estimate.
            if pnl is None:
                if record.side == "BUY":
                    pnl = (exit_price - record.entry_price) * qty
                else:
                    pnl = (record.entry_price - exit_price) * qty

            entry_price = record.entry_price or 0
            if entry_price:
                pnl_pct = (pnl / (entry_price * qty)) * 100 if qty else 0.0
            else:
                pnl_pct = 0.0  # guard against zero/absent entry price

            # Calculate hold duration
            hold_duration = (datetime.now() - record.entry_time).total_seconds() / 60

            # Update record
            record.exit_price = exit_price
            record.exit_quantity = qty
            record.exit_time = datetime.now()
            record.exit_reason = exit_reason
            record.profit_loss = pnl
            record.profit_loss_pct = pnl_pct
            record.mae = mae
            record.mfe = mfe
            record.hold_duration_minutes = int(hold_duration)
            record.status = "closed"
            
            self._session.commit()
            logger.info(f"Closed trade {trade_id}: P&L = ₹{pnl:.2f} ({pnl_pct:.2f}%)")
            return True
            
        except Exception as e:
            logger.error(f"Failed to close trade: {e}")
            self._session.rollback()
            return False
    
    def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        """Get trade details by ID."""
        try:
            record = self._session.query(TradeRecord).filter_by(trade_id=trade_id).first()
            if record:
                return self._record_to_dict(record)
        except Exception as e:
            logger.error(f"Failed to get trade: {e}")
        return None
    
    def get_trades_by_date(
        self,
        start_date: datetime,
        end_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get trades within a date range."""
        try:
            query = self._session.query(TradeRecord).filter(
                TradeRecord.entry_time >= start_date
            )
            
            if end_date:
                query = query.filter(TradeRecord.entry_time <= end_date)
            
            records = query.order_by(TradeRecord.entry_time.desc()).all()
            return [self._record_to_dict(r) for r in records]
            
        except Exception as e:
            logger.error(f"Failed to get trades by date: {e}")
            return []
    
    def get_trades_by_strategy(self, strategy: str) -> list[dict[str, Any]]:
        """Get all trades for a specific strategy."""
        try:
            records = self._session.query(TradeRecord).filter_by(
                strategy=strategy
            ).order_by(TradeRecord.entry_time.desc()).all()
            return [self._record_to_dict(r) for r in records]
        except Exception as e:
            logger.error(f"Failed to get trades by strategy: {e}")
            return []
    
    def get_performance_summary(
        self,
        start_date: datetime | None = None,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """
        Calculate performance summary statistics.
        
        Returns aggregated statistics for closed trades.
        """
        try:
            query = self._session.query(TradeRecord).filter_by(status="closed")
            
            if start_date:
                query = query.filter(TradeRecord.entry_time >= start_date)
            if strategy:
                query = query.filter_by(strategy=strategy)
            
            trades = query.all()
            
            if not trades:
                return {"message": "No closed trades found"}
            
            total_pnl = sum(t.profit_loss or 0 for t in trades)
            winners = [t for t in trades if (t.profit_loss or 0) > 0]
            losers = [t for t in trades if (t.profit_loss or 0) < 0]
            
            return {
                "total_trades": len(trades),
                "winners": len(winners),
                "losers": len(losers),
                "win_rate": len(winners) / len(trades) * 100 if trades else 0,
                "total_pnl": total_pnl,
                "avg_pnl": total_pnl / len(trades) if trades else 0,
                "avg_winner": sum(t.profit_loss for t in winners) / len(winners) if winners else 0,
                "avg_loser": sum(t.profit_loss for t in losers) / len(losers) if losers else 0,
                "avg_hold_duration": sum(t.hold_duration_minutes or 0 for t in trades) / len(trades) if trades else 0,
                "best_trade": max(t.profit_loss for t in trades) if trades else 0,
                "worst_trade": min(t.profit_loss for t in trades) if trades else 0,
            }
            
        except Exception as e:
            logger.error(f"Failed to calculate performance: {e}")
            return {"error": str(e)}
    
    def log_decision(
        self,
        workflow_id: str,
        agent: str,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        decision: str,
        confidence: float,
        reasoning: str,
        latency_ms: int,
    ) -> None:
        """Log an agent decision for replay."""
        try:
            log = DecisionLog(
                workflow_id=workflow_id,
                agent=agent,
                input_data=json.dumps(input_data, default=str),
                output_data=json.dumps(output_data, default=str),
                decision=decision,
                confidence=confidence,
                reasoning=reasoning,
                latency_ms=latency_ms,
            )
            self._session.add(log)
            self._session.commit()
        except Exception as e:
            logger.error(f"Failed to log decision: {e}")
            self._session.rollback()
    
    def _record_to_dict(self, record: TradeRecord) -> dict[str, Any]:
        """Convert SQLAlchemy record to dictionary."""
        return {
            "trade_id": record.trade_id,
            "signal_id": record.signal_id,
            "workflow_id": record.workflow_id,
            "symbol": record.symbol,
            "exchange": record.exchange,
            "side": record.side,
            "strategy": record.strategy,
            "entry_price": record.entry_price,
            "entry_quantity": record.entry_quantity,
            "entry_time": record.entry_time.isoformat() if record.entry_time else None,
            "exit_price": record.exit_price,
            "exit_time": record.exit_time.isoformat() if record.exit_time else None,
            "exit_reason": record.exit_reason,
            "stop_loss": record.stop_loss,
            "target_price": record.target_price,
            "profit_loss": record.profit_loss,
            "profit_loss_pct": record.profit_loss_pct,
            "mae": record.mae,
            "mfe": record.mfe,
            "hold_duration_minutes": record.hold_duration_minutes,
            "regime": record.regime,
            "regime_confidence": record.regime_confidence,
            "status": record.status,
        }
    
    def _extract_decision_chain(self, state: dict[str, Any]) -> dict[str, Any]:
        """Extract decision chain from state for audit trail."""
        return {
            "regime": {
                "value": state.get("regime"),
                "confidence": state.get("regime_confidence"),
                "reasoning": state.get("regime_reasoning"),
            },
            "strategies": {
                "active": state.get("active_strategies"),
                "reasoning": state.get("strategy_reasoning"),
            },
            "signal_validation": {
                "validated_count": len(state.get("validated_signals", [])),
                "rejected_count": len(state.get("rejected_signals", [])),
            },
            "risk": {
                "approved_count": len(state.get("approved_trades", [])),
                "rejected_count": len(state.get("risk_rejected", [])),
                "warnings": state.get("risk_warnings", []),
            },
        }
