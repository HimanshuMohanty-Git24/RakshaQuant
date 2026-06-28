"""
Mistake Classifier Module

Classifies trade mistakes using LLM reasoning combined with rule-based logic.
Converts trade outcomes into actionable lessons for the memory system.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from src.config import get_settings

from .analyzer import TradeOutcome

logger = logging.getLogger(__name__)


class MistakeCategory:
    """Categories of trading mistakes."""

    REGIME_MISMATCH = "regime_mismatch"
    STRATEGY_MISMATCH = "strategy_mismatch"
    POOR_TIMING = "poor_timing"
    OVERTRADING = "overtrading"
    POSITION_SIZING = "position_sizing"
    STOP_LOSS_TOO_TIGHT = "stop_loss_too_tight"
    STOP_LOSS_TOO_LOOSE = "stop_loss_too_loose"
    PREMATURE_EXIT = "premature_exit"
    LATE_EXIT = "late_exit"
    CHASING = "chasing"
    SIGNAL_QUALITY = "signal_quality"


CLASSIFIER_SYSTEM_PROMPT = """You are a Trading Mistake Classifier for an automated trading system.

Your job is to analyze losing or underperforming trades and identify what went wrong.
This helps the system learn from mistakes and avoid repeating them.

For each trade outcome, you should:
1. Identify the primary mistake category
2. Provide a clear, actionable description
3. Suggest how to avoid this in the future
4. Rate the severity (low, medium, high, critical)

Mistake categories:
- regime_mismatch: Strategy was wrong for market conditions
- strategy_mismatch: Strategy parameters were inappropriate
- poor_timing: Entry/exit timing was suboptimal
- overtrading: Trade was unnecessary or low quality
- position_sizing: Position size was inappropriate
- stop_loss_too_tight: Stop was hit but trade would have won
- stop_loss_too_loose: Stop was too far, loss was larger than needed
- premature_exit: Exited too early, missed gains
- late_exit: Held too long, gains became losses
- chasing: Entered too late after the move started
- signal_quality: Signal was weak or false

Respond with JSON:
{
    "category": "mistake_category",
    "severity": "low|medium|high|critical",
    "description": "What went wrong in 1-2 sentences",
    "lesson": "Actionable lesson for the future in 1 sentence",
    "context_factors": ["list", "of", "relevant", "factors"]
}

Be specific and practical. Focus on what the SYSTEM could do differently, not market luck."""


@dataclass
class ClassifiedMistake:
    """A classified trading mistake."""

    lesson_id: str
    trade_id: str
    category: str
    severity: str
    description: str
    lesson: str
    context_factors: list[str]
    trade_context: dict[str, Any]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "trade_id": self.trade_id,
            "category": self.category,
            "severity": self.severity,
            "description": self.description,
            "lesson": self.lesson,
            "context_factors": self.context_factors,
            "trade_context": self.trade_context,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class MistakeClassifier:
    """
    Classifies trading mistakes using LLM + rules.

    Uses a combination of rule-based pre-analysis and LLM reasoning
    to generate actionable learning lessons.
    """

    _llm: ChatGroq = None

    def __post_init__(self):
        self._initialize_llm()

    def _initialize_llm(self) -> None:
        """Initialize the LLM for classification."""
        settings = get_settings()
        self._llm = ChatGroq(
            api_key=settings.groq_api_key.get_secret_value(),
            model_name=settings.groq_model_fallback,  # Use faster model for classification
            temperature=0.2,
            max_tokens=512,
        )

    def classify(self, outcome: TradeOutcome) -> ClassifiedMistake | None:
        """
        Classify a trade outcome as a mistake (if applicable).

        Only generates lessons for losing trades or significantly
        underperforming winners.

        Args:
            outcome: Analyzed trade outcome

        Returns:
            ClassifiedMistake if a lesson should be generated
        """
        # Check if this trade warrants classification
        if not self._should_classify(outcome):
            logger.debug(f"Trade {outcome.trade_id} doesn't warrant classification")
            return None

        # First, apply rule-based pre-classification
        rule_based = self._rule_based_classify(outcome)

        # Then, get LLM classification for context
        llm_result = self._llm_classify(outcome)

        # Merge results (prefer rule-based category if clear, LLM for context)
        return self._merge_classifications(outcome, rule_based, llm_result)

    def classify_batch(
        self,
        outcomes: list[TradeOutcome],
    ) -> list[ClassifiedMistake]:
        """Classify multiple trade outcomes."""
        mistakes = []

        for outcome in outcomes:
            mistake = self.classify(outcome)
            if mistake:
                mistakes.append(mistake)

        return mistakes

    def _should_classify(self, outcome: TradeOutcome) -> bool:
        """Determine if a trade should be classified."""

        # Always classify losers
        if not outcome.is_winner:
            return True

        # Classify inefficient winners (captured less than 50% of MFE)
        if outcome.efficiency < 0.5:
            return True

        # Classify premature exits
        if outcome.was_premature_exit:
            return True

        # Classify late exits (had significant MFE but small profit)
        if outcome.was_late_exit:
            return True

        return False

    def _rule_based_classify(
        self,
        outcome: TradeOutcome,
    ) -> dict[str, Any]:
        """Apply rule-based classification."""

        category = None
        severity = "medium"
        description = ""

        # Rule 1: Stop loss hit immediately
        if outcome.hit_stop_loss and outcome.hold_duration_minutes < 10:
            category = MistakeCategory.STOP_LOSS_TOO_TIGHT
            severity = "high"
            description = f"Stop loss hit within {outcome.hold_duration_minutes} minutes"

        # Rule 2: Large loss without hitting stop
        elif not outcome.hit_stop_loss and outcome.profit_loss_pct < -3:
            category = MistakeCategory.STOP_LOSS_TOO_LOOSE
            severity = "high"
            description = f"Loss of {outcome.profit_loss_pct:.1f}% without stop loss trigger"

        # Rule 3: Premature exit on winner
        elif outcome.was_premature_exit:
            category = MistakeCategory.PREMATURE_EXIT
            severity = "medium"
            description = f"Exited with {outcome.efficiency:.0%} efficiency despite winning"

        # Rule 4: Late exit (had gains, ended in loss)
        elif outcome.was_late_exit:
            category = MistakeCategory.LATE_EXIT
            severity = "high"
            description = f"Had MFE of {outcome.mfe:.2f} but ended with loss"

        # Rule 5: Very short hold time
        elif outcome.hold_duration_minutes < 5 and not outcome.is_winner:
            category = MistakeCategory.POOR_TIMING
            severity = "medium"
            description = "Trade exited within 5 minutes with loss"

        return {
            "category": category,
            "severity": severity,
            "description": description,
        }

    def _llm_classify(
        self,
        outcome: TradeOutcome,
    ) -> dict[str, Any]:
        """Get LLM classification for additional context."""

        try:
            # Build context for LLM
            context = f"""
## Trade Outcome to Classify

- Trade ID: {outcome.trade_id}
- Symbol: {outcome.symbol}
- Strategy: {outcome.strategy}
- Market Regime: {outcome.regime}
- Result: {"WINNER" if outcome.is_winner else "LOSER"}
- P&L: {outcome.profit_loss:.2f} ({outcome.profit_loss_pct:.2f}%)

### Execution Details
- Hold Duration: {outcome.hold_duration_minutes} minutes
- Maximum Adverse Excursion (MAE): {outcome.mae:.2f}
- Maximum Favorable Excursion (MFE): {outcome.mfe:.2f}
- Efficiency: {outcome.efficiency:.0%} (how much of max gain was captured)

### Exit Analysis
- Hit Stop Loss: {outcome.hit_stop_loss}
- Hit Target: {outcome.hit_target}
- Premature Exit: {outcome.was_premature_exit}
- Late Exit: {outcome.was_late_exit}

Analyze what went wrong and provide a lesson.
"""

            messages = [
                SystemMessage(content=CLASSIFIER_SYSTEM_PROMPT),
                HumanMessage(content=context),
            ]

            response = self._llm.invoke(messages)
            return self._parse_llm_response(response.content)

        except Exception as e:
            logger.error(f"LLM classification failed: {e}")
            return {}

    def _parse_llm_response(self, content: str) -> dict[str, Any]:
        """Parse LLM JSON response."""
        try:
            content = content.strip()

            if "```json" in content:
                start = content.find("```json") + 7
                end = content.find("```", start)
                content = content[start:end].strip()
            elif "```" in content:
                start = content.find("```") + 3
                end = content.find("```", start)
                content = content[start:end].strip()

            return json.loads(content)

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response: {e}")
            return {}

    def _merge_classifications(
        self,
        outcome: TradeOutcome,
        rule_based: dict[str, Any],
        llm_result: dict[str, Any],
    ) -> ClassifiedMistake:
        """Merge rule-based and LLM classifications."""

        # Prefer rule-based category if identified
        category = rule_based.get("category") or llm_result.get(
            "category", MistakeCategory.SIGNAL_QUALITY
        )
        severity = rule_based.get("severity") or llm_result.get("severity", "medium")

        # Prefer LLM description if available (more context-aware)
        description = llm_result.get("description") or rule_based.get(
            "description", "Trade underperformed"
        )

        # Get lesson from LLM
        lesson = llm_result.get("lesson", f"Review {category} conditions before similar trades")

        # Get context factors
        context_factors = llm_result.get("context_factors", [outcome.strategy, outcome.regime])

        return ClassifiedMistake(
            lesson_id=f"LSN-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}",
            trade_id=outcome.trade_id,
            category=category,
            severity=severity,
            description=description,
            lesson=lesson,
            context_factors=context_factors,
            trade_context={
                "symbol": outcome.symbol,
                "strategy": outcome.strategy,
                "regime": outcome.regime,
                "pnl_pct": outcome.profit_loss_pct,
                "efficiency": outcome.efficiency,
            },
            created_at=datetime.now(),
        )
