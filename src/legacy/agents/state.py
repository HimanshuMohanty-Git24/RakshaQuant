"""
Trading State Module

Defines the shared state schema for all agents in the trading workflow.
Uses TypedDict for type-safe state management with LangGraph.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


class MarketRegime(Enum):
    """Market regime classifications."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


class DecisionType(Enum):
    """Types of agent decisions."""

    APPROVE = "approve"
    REJECT = "reject"
    HOLD = "hold"
    MODIFY = "modify"


@dataclass
class AgentDecision:
    """Represents a decision made by an agent."""

    agent_name: str
    decision: DecisionType
    confidence: float  # 0-1
    reasoning: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "decision": self.decision.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class MemoryLesson:
    """A lesson learned from past trading mistakes."""

    lesson_id: str
    category: str  # e.g., "regime_mismatch", "overtrading", "poor_timing"
    description: str
    severity: str  # "low", "medium", "high", "critical"
    context: dict[str, Any]
    created_at: datetime
    relevance_score: float = 1.0  # Decays over time

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "category": self.category,
            "description": self.description,
            "severity": self.severity,
            "context": self.context,
            "created_at": self.created_at.isoformat(),
            "relevance_score": self.relevance_score,
        }


class TradingState(TypedDict):
    """
    Shared state for the trading agent workflow.

    This state is passed between all agents in the LangGraph workflow.
    Each agent can read from and write to specific fields.
    """

    # ===========================================
    # Input Data (from Market Layer)
    # ===========================================

    # Current market data and indicators
    market_data: dict[str, Any]  # Symbol -> price data
    indicators: dict[str, Any]  # Symbol -> calculated indicators

    # Raw signals from signal engine
    signals: list[dict[str, Any]]

    # ===========================================
    # Agent Decisions
    # ===========================================

    # Current market regime (set by Market Regime Agent)
    regime: str  # MarketRegime value
    regime_confidence: float
    regime_reasoning: str

    # Active strategies (set by Strategy Selection Agent)
    active_strategies: list[str]
    strategy_reasoning: str

    # Validated signals (set by Signal Validation Agent)
    validated_signals: list[dict[str, Any]]
    rejected_signals: list[dict[str, Any]]

    # Risk approval (set by Risk & Compliance Agent)
    approved_trades: list[dict[str, Any]]
    risk_rejected: list[dict[str, Any]]
    risk_warnings: list[str]

    # ===========================================
    # Memory & Learning
    # ===========================================

    # Lessons from past mistakes (injected before agent runs)
    memory_lessons: list[dict[str, Any]]

    # ===========================================
    # Execution State
    # ===========================================

    # Final trade decisions ready for execution
    trades_to_execute: list[dict[str, Any]]

    # Current portfolio state
    portfolio: dict[str, Any]

    # Daily trading stats
    daily_stats: dict[str, Any]

    # ===========================================
    # Agent Communication (LangChain messages)
    # ===========================================

    # Message history for agent reasoning traces
    messages: Annotated[list[BaseMessage], add_messages]

    # ===========================================
    # Metadata
    # ===========================================

    # Workflow metadata
    workflow_id: str
    timestamp: str
    errors: list[str]

    # ===========================================
    # Sentiment & Prediction (Free Tier)
    # ===========================================

    # News sentiment from NewsAnalyst.
    # Canonical contract: {"avg_sentiment": float} (empty dict {} when unavailable).
    news_sentiment: dict[str, Any]

    # Recent news headlines from NewsAnalyst: list of {"title": str, "sentiment": str}.
    news_headlines: list[dict[str, Any]]

    # Market Mood Index from SentimentAgent.
    # Canonical contract: SentimentSignal.to_dict() (keys: mood_index, mood_label,
    # news_score, volatility_score, breadth_score, ...) or {} when unavailable.
    market_mood: dict[str, Any]

    # Price predictions from PredictionAgent
    prediction_signals: list[dict[str, Any]]


def create_initial_state(
    workflow_id: str | None = None,
) -> TradingState:
    """
    Create an initial empty trading state.

    Args:
        workflow_id: Optional workflow identifier

    Returns:
        Initialized TradingState with default values
    """
    if workflow_id is None:
        workflow_id = f"WF-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    return TradingState(
        # Input data
        market_data={},
        indicators={},
        signals=[],
        # Regime
        regime=MarketRegime.UNKNOWN.value,
        regime_confidence=0.0,
        regime_reasoning="",
        # Strategies
        active_strategies=[],
        strategy_reasoning="",
        # Signals
        validated_signals=[],
        rejected_signals=[],
        # Risk
        approved_trades=[],
        risk_rejected=[],
        risk_warnings=[],
        # Memory
        memory_lessons=[],
        # Execution
        trades_to_execute=[],
        portfolio={},
        daily_stats={
            "trades_count": 0,
            "profit_loss": 0.0,
            "max_drawdown": 0.0,
        },
        # Messages
        messages=[],
        # Metadata
        workflow_id=workflow_id,
        timestamp=datetime.now().isoformat(),
        errors=[],
        # Sentiment & Prediction (Free Tier)
        news_sentiment={},
        news_headlines=[],
        market_mood={},
        prediction_signals=[],
    )
