"""
Strategy Selection Agent Module

Selects active trading strategies based on the current market regime
and historical performance data.

Features:
- Rate limiting to prevent API throttling
- Circuit breaker for resilience
- Structured error handling
"""

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from src.config import get_settings
from src.finops import record_llm_response
from src.utils.circuit_breaker import CircuitBreakerOpenError, get_groq_circuit_breaker
from src.utils.rate_limiter import get_groq_limiter

from .state import TradingState

logger = logging.getLogger(__name__)


STRATEGY_SYSTEM_PROMPT = """You are a Strategy Selection Agent for an automated trading system.

Your role is to select which trading strategies should be active based on:
1. The current market regime (trending_up, trending_down, ranging, volatile)
2. Historical performance of each strategy in similar conditions
3. Memory lessons from past strategy selection mistakes

Available strategies:
- momentum: Best in trending markets with strong directional moves
- mean_reversion: Best in ranging markets with clear support/resistance
- breakout: Best when volatility is low and a breakout is anticipated
- trend_following: Best in established trends with high ADX

Consider:
- Avoid strategies that historically underperformed in the current regime
- Don't be too aggressive - selecting 1-2 strategies is often better than all 4
- Weight memory lessons heavily - past mistakes should inform current decisions
- Lean on the REAL historical win-rates provided (not assumptions); prefer strategies with a
  proven edge in this regime, and be cautious when win-rates are only prior estimates

Respond with JSON:
{
    "active_strategies": ["list", "of", "strategy", "names"],
    "reasoning": "Explanation of selection with specific reference to regime and lessons",
    "strategy_notes": {
        "strategy_name": "why selected or rejected"
    }
}"""


def create_strategy_agent() -> ChatGroq:
    """Create the strategy selection agent."""
    settings = get_settings()

    return ChatGroq(
        api_key=settings.groq_api_key.get_secret_value(),
        model_name=settings.groq_model_primary,
        temperature=settings.groq_temperature,
        max_tokens=1024,
    )


def strategy_selection_node(state: TradingState) -> dict[str, Any]:
    """
    LangGraph node for strategy selection.

    Selects which strategies should be active based on regime and memory.
    Uses rate limiting and circuit breaker for resilience.

    Args:
        state: Current trading state with regime and memory lessons

    Returns:
        State updates with active strategies
    """
    logger.info("Running Strategy Selection Agent...")

    settings = get_settings()
    rate_limiter = get_groq_limiter()
    circuit_breaker = get_groq_circuit_breaker()

    try:
        regime = state.get("regime", "unknown")
        regime_confidence = state.get("regime_confidence", 0.0)
        memory_lessons = state.get("memory_lessons", [])
        daily_stats = state.get("daily_stats", {})

        # Filter relevant lessons
        strategy_lessons = [
            lesson
            for lesson in memory_lessons
            if lesson.get("category") in ["strategy_mismatch", "overtrading"]
        ]

        # Build context
        context = _build_strategy_context(regime, regime_confidence, strategy_lessons, daily_stats)

        messages = [
            SystemMessage(content=STRATEGY_SYSTEM_PROMPT),
            HumanMessage(content=context),
        ]

        # Check circuit breaker
        if not circuit_breaker.is_available:
            raise CircuitBreakerOpenError("groq_api", circuit_breaker.recovery_time)

        # Apply rate limiting
        if settings.enable_rate_limiting:
            rate_limiter.acquire_sync()

        def invoke_llm():
            agent = create_strategy_agent()
            return agent.invoke(messages)

        response = circuit_breaker.call(invoke_llm)
        record_llm_response("strategy_selection", response, model=settings.groq_model_primary)
        result = _parse_strategy_response(response.content)

        logger.info(f"Selected strategies: {result['active_strategies']}")

        return {
            "active_strategies": result["active_strategies"],
            "strategy_reasoning": result["reasoning"],
            "messages": [response],
        }

    except CircuitBreakerOpenError as e:
        logger.warning(f"Circuit breaker open: {e}")
        return _fallback_strategy_selection(state, regime, str(e))

    except Exception as e:
        logger.error(f"Strategy Selection Agent error: {e}")
        return _fallback_strategy_selection(state, state.get("regime", "unknown"), str(e))


def _fallback_strategy_selection(
    state: TradingState, regime: str, error_msg: str
) -> dict[str, Any]:
    """
    Fallback strategy selection based on regime.

    Used when LLM is unavailable.
    """
    # Default strategies per regime
    regime_strategies = {
        "trending_up": ["momentum", "trend_following"],
        "trending_down": ["trend_following"],
        "ranging": ["mean_reversion"],
        "volatile": ["breakout"],
    }

    strategies = regime_strategies.get(regime, ["trend_following"])

    logger.info(f"Using fallback strategies for {regime}: {strategies}")

    return {
        "active_strategies": strategies,
        "strategy_reasoning": f"Fallback selection for {regime} regime. Error: {error_msg}",
        "errors": state.get("errors", []) + [f"Strategy Agent fallback: {error_msg}"],
    }


def _build_strategy_context(
    regime: str,
    regime_confidence: float,
    lessons: list[dict[str, Any]],
    daily_stats: dict[str, Any],
) -> str:
    """Build context for strategy selection."""

    context_parts = [
        "## Current Market Regime\n",
        f"- Regime: **{regime}**",
        f"- Confidence: {regime_confidence:.2f}",
        "\n## Today's Trading Stats\n",
        f"- Trades executed: {daily_stats.get('trades_count', 0)}",
        f"- P&L: {daily_stats.get('profit_loss', 0):.2f}",
    ]

    # Real strategy performance from tracker (replaces hardcoded data)
    context_parts.append("\n## Historical Strategy Performance by Regime\n")
    try:
        from src.memory.performance_tracker import get_performance_tracker

        tracker = get_performance_tracker()
        performance_data = tracker.get_all_strategy_performance(regime)
        for strategy, win_rate in sorted(
            performance_data.items(), key=lambda x: x[1], reverse=True
        ):
            perf = tracker.get_strategy_performance(strategy, regime)
            if perf.total_trades > 0:
                context_parts.append(
                    f"- {strategy}: {win_rate:.0%} win rate ({perf.total_trades} trades)"
                )
            else:
                context_parts.append(f"- {strategy}: {win_rate:.0%} win rate (prior estimate)")
    except Exception as e:
        logger.warning(f"Performance tracker unavailable, using defaults: {e}")
        default_data = {
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
        if regime in default_data:
            for strategy, win_rate in default_data[regime].items():
                context_parts.append(f"- {strategy}: {win_rate:.0%} win rate (default)")

    # Add lessons
    if lessons:
        context_parts.append("\n## Past Mistakes to Avoid\n")
        for lesson in lessons[:3]:
            context_parts.append(
                f"- [{lesson.get('severity', 'N/A')}] {lesson.get('description', 'N/A')}"
            )

    return "\n".join(context_parts)


def _parse_strategy_response(content: str) -> dict[str, Any]:
    """Parse strategy selection response."""

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

        result = json.loads(content)

        # Validate strategies
        valid_strategies = ["momentum", "mean_reversion", "breakout", "trend_following"]
        result["active_strategies"] = [
            s for s in result.get("active_strategies", []) if s in valid_strategies
        ]

        if not result["active_strategies"]:
            result["active_strategies"] = ["trend_following"]

        if "reasoning" not in result:
            result["reasoning"] = "No reasoning provided"

        return result

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse strategy response: {e}")
        return {
            "active_strategies": ["trend_following"],
            "reasoning": f"Parse error, defaulting to trend_following: {content[:200]}",
        }
