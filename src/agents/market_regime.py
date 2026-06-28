"""
Market Regime Agent Module

Classifies the current market regime based on volatility, trend, and market conditions.
Runs on a slow cadence and provides context for strategy selection.

Features:
- Rate limiting to prevent API throttling
- Circuit breaker for resilience
- Structured error handling
"""

import json
import logging
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from src.config import get_settings
from src.finops import record_llm_response
from src.utils.rate_limiter import get_groq_limiter
from src.utils.circuit_breaker import get_groq_circuit_breaker, CircuitBreakerOpenError
from src.utils.errors import RateLimitError, LLMResponseError
from .state import MarketRegime, TradingState

logger = logging.getLogger(__name__)


REGIME_SYSTEM_PROMPT = """You are a Market Regime Classification Agent for an automated trading system.

Your role is to analyze market conditions and classify the current regime into one of these categories:
- trending_up: Strong bullish trend with consistent higher highs and higher lows
- trending_down: Strong bearish trend with consistent lower highs and lower lows  
- ranging: Price moving sideways within a defined range, no clear trend
- volatile: High volatility with erratic price movements, uncertain direction

You will receive:
1. Market indicators (ADX, RSI, volatility metrics, moving averages)
2. Recent price action summary
3. Any relevant memory lessons from past regime misclassifications

Respond with a JSON object containing:
{
    "regime": "one of: trending_up, trending_down, ranging, volatile",
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation of why this regime was chosen",
    "key_factors": ["list", "of", "key", "indicators", "considered"]
}

Weigh ALL the context you are given, not just price: news sentiment and the Market Mood Index
are leading indicators of regime shifts, and the ML prediction consensus is corroborating
evidence — when they disagree with the price-based read, lower your confidence.

Calibrate confidence to genuine probability, not enthusiasm: reserve >0.8 only when multiple
independent factors (trend indicators + sentiment + predictions) agree; use 0.4–0.6 for a
single weak signal; prefer "ranging" with lower confidence when unsure.
Consider the memory lessons carefully to avoid past mistakes."""


def create_regime_agent() -> ChatGroq:
    """Create the market regime classification agent."""
    settings = get_settings()
    
    return ChatGroq(
        api_key=settings.groq_api_key.get_secret_value(),
        model_name=settings.groq_model_primary,
        temperature=settings.groq_temperature,
        max_tokens=1024,
    )


def market_regime_node(state: TradingState) -> dict[str, Any]:
    """
    LangGraph node for market regime classification.
    
    Analyzes current market conditions and classifies the regime.
    Uses rate limiting and circuit breaker for resilience.
    
    Args:
        state: Current trading state with market data and indicators
        
    Returns:
        State updates with regime classification
    """
    logger.info("Running Market Regime Agent...")
    
    settings = get_settings()
    rate_limiter = get_groq_limiter()
    circuit_breaker = get_groq_circuit_breaker()
    
    try:
        # Extract relevant data for regime analysis
        indicators = state.get("indicators", {})
        market_data = state.get("market_data", {})
        memory_lessons = state.get("memory_lessons", [])
        
        # Filter lessons relevant to regime classification
        regime_lessons = [
            lesson for lesson in memory_lessons
            if lesson.get("category") == "regime_mismatch"
        ]
        
        # Build context for the agent (enriched with support agent outputs)
        context = _build_regime_context_enriched(indicators, market_data, regime_lessons, state)
        
        messages = [
            SystemMessage(content=REGIME_SYSTEM_PROMPT),
            HumanMessage(content=context),
        ]
        
        # Check circuit breaker state
        if not circuit_breaker.is_available:
            raise CircuitBreakerOpenError("groq_api", circuit_breaker.recovery_time)
        
        # Apply rate limiting before LLM call
        if settings.enable_rate_limiting:
            rate_limiter.acquire_sync()
        
        # Try primary model first, fallback on rate limit
        models_to_try = [settings.groq_model_primary, settings.groq_model_fallback]
        
        response = None
        last_error = None
        
        for model_name in models_to_try:
            try:
                def invoke_llm():
                    agent = ChatGroq(
                        api_key=settings.groq_api_key.get_secret_value(),
                        model_name=model_name,
                        temperature=settings.groq_temperature,
                        max_tokens=1024,
                    )
                    return agent.invoke(messages)
                
                # Use circuit breaker for the LLM call
                response = circuit_breaker.call(invoke_llm)
                logger.info(f"Regime agent using model: {model_name}")
                break
                
            except Exception as model_error:
                last_error = model_error
                error_str = str(model_error).lower()
                
                if "rate_limit" in error_str or "429" in error_str:
                    logger.warning(f"Rate limit on {model_name}, trying fallback...")
                    # Wait a bit before trying fallback
                    if settings.enable_rate_limiting:
                        import time
                        time.sleep(2)
                    continue
                elif isinstance(model_error, CircuitBreakerOpenError):
                    raise
                else:
                    logger.error(f"LLM error on {model_name}: {model_error}")
                    continue
        
        if response is None:
            if last_error:
                raise last_error
            raise RateLimitError("groq", retry_after=60.0)

        # Record token usage / cost for FinOps (never raises).
        record_llm_response("market_regime", response, model=model_name)

        # Parse response
        result = _parse_regime_response(response.content)
        
        logger.info(f"Regime classified as: {result['regime']} (confidence: {result['confidence']:.2f})")
        
        return {
            "regime": result["regime"],
            "regime_confidence": result["confidence"],
            "regime_reasoning": result["reasoning"],
            "messages": [response],
        }
        
    except CircuitBreakerOpenError as e:
        logger.warning(f"Circuit breaker open: {e}")
        return _fallback_regime_classification(state, f"Circuit breaker open: {e}")
        
    except RateLimitError as e:
        logger.warning(f"Rate limit exceeded: {e}")
        return _fallback_regime_classification(state, f"Rate limited: {e}")
        
    except Exception as e:
        logger.error(f"Market Regime Agent error: {e}")
        return _fallback_regime_classification(state, str(e))


def _fallback_regime_classification(state: TradingState, error_msg: str) -> dict[str, Any]:
    """
    Fallback regime classification based on market data.
    
    Used when LLM is unavailable due to rate limits or errors.
    """
    market_data = state.get("market_data", {})
    avg_change = 0.0
    count = 0
    
    for data in market_data.values():
        if isinstance(data, dict):
            change = data.get("change_percent", 0)
            if change is not None:
                avg_change += change
                count += 1
    
    if count > 0:
        avg_change /= count
    
    # Infer regime from average market change
    if avg_change > 0.5:
        regime = "trending_up"
        confidence = 0.5
    elif avg_change < -0.5:
        regime = "trending_down"
        confidence = 0.5
    else:
        regime = "ranging"
        confidence = 0.4
        
    logger.info(f"Using fallback regime: {regime} (inferred from avg change: {avg_change:.2f}%)")
    
    return {
        "regime": regime,
        "regime_confidence": confidence,
        "regime_reasoning": f"Fallback: Inferred from average market change ({avg_change:.2f}%). Error: {error_msg}",
        "errors": state.get("errors", []) + [f"Regime Agent fallback: {error_msg}"],
    }


def _build_regime_context(
    indicators: dict[str, Any],
    market_data: dict[str, Any],
    lessons: list[dict[str, Any]],
) -> str:
    """Build context string for regime classification."""
    
    context_parts = ["## Current Market Analysis\n"]
    
    # Add indicator summary
    if indicators:
        context_parts.append("### Technical Indicators\n")
        for symbol, ind in indicators.items():
            context_parts.append(f"\n**{symbol}**:")
            
            # Trend indicators
            if "trend" in ind:
                trend = ind["trend"]
                context_parts.append(f"- ADX: {trend.get('adx', 'N/A')}")
                context_parts.append(f"- +DI: {trend.get('plus_di', 'N/A')}, -DI: {trend.get('minus_di', 'N/A')}")
            
            # Momentum
            if "momentum" in ind:
                mom = ind["momentum"]
                context_parts.append(f"- RSI: {mom.get('rsi', 'N/A')}")
            
            # Moving averages
            if "moving_averages" in ind:
                ma = ind["moving_averages"]
                if "sma" in ma:
                    sma_str = ", ".join([f"SMA{k}={v:.2f}" for k, v in ma["sma"].items()])
                    context_parts.append(f"- {sma_str}")
            
            # Volatility
            if "volatility" in ind:
                vol = ind["volatility"]
                context_parts.append(f"- ATR: {vol.get('atr', 'N/A')}")
                context_parts.append(f"- BB Width: {vol.get('bb_percent', 'N/A')}")
    
    # Add price summary
    if market_data:
        context_parts.append("\n### Price Summary\n")
        for symbol, data in market_data.items():
            if isinstance(data, dict):
                context_parts.append(
                    f"- {symbol}: Close={data.get('close', 'N/A')}, "
                    f"Change={data.get('change_percent', 'N/A')}%"
                )
    
    # Add memory lessons
    if lessons:
        context_parts.append("\n### Past Lessons (Avoid These Mistakes)\n")
        for lesson in lessons[:3]:  # Top 3 most relevant
            context_parts.append(
                f"- [{lesson.get('severity', 'N/A')}] {lesson.get('description', 'N/A')}"
            )
    
    return "\n".join(context_parts)


def _build_regime_context_enriched(
    indicators: dict[str, Any],
    market_data: dict[str, Any],
    lessons: list[dict[str, Any]],
    state: dict[str, Any],
) -> str:
    """Build enriched context string using support agent outputs."""
    
    # Start with the base context
    base_context = _build_regime_context(indicators, market_data, lessons)
    
    enrichment_parts = []
    
    # Add news sentiment from news analyst agent.
    # Contract: news_sentiment is a dict {"avg_sentiment": float} (see state.py).
    news_sentiment = state.get("news_sentiment") or {}
    news_headlines = state.get("news_headlines", [])
    avg_sentiment = news_sentiment.get("avg_sentiment") if isinstance(news_sentiment, dict) else None
    if avg_sentiment is not None:
        enrichment_parts.append("\n### News Sentiment Analysis\n")
        enrichment_parts.append(
            f"- Overall News Sentiment Score: {avg_sentiment:.2f} (-1 bearish to +1 bullish)"
        )
        if news_headlines:
            enrichment_parts.append("- Key Headlines:")
            for headline in news_headlines[:5]:
                if isinstance(headline, dict):
                    enrichment_parts.append(
                        f"  - [{headline.get('sentiment', 'neutral')}] {headline.get('title', 'N/A')}"
                    )
                elif isinstance(headline, str):
                    enrichment_parts.append(f"  - {headline}")

    # Add market mood from sentiment agent.
    # Contract: market_mood is SentimentSignal.to_dict() (see state.py).
    market_mood = state.get("market_mood") or {}
    if isinstance(market_mood, dict) and market_mood.get("mood_index") is not None:
        enrichment_parts.append("\n### Market Mood Index\n")
        enrichment_parts.append(f"- Mood Score: {float(market_mood['mood_index']):.0f}/100 (0-100 scale)")
        mood_label = market_mood.get("mood_label")
        if mood_label:
            enrichment_parts.append(f"- Mood Label: {mood_label}")
        for key in ("news_score", "volatility_score", "breadth_score"):
            if market_mood.get(key) is not None:
                enrichment_parts.append(f"- {key}: {market_mood[key]}")
    
    # Add prediction signals from prediction agent
    prediction_signals = state.get("prediction_signals", [])
    if prediction_signals:
        enrichment_parts.append("\n### ML Prediction Consensus\n")
        for pred in prediction_signals[:5]:
            if isinstance(pred, dict):
                enrichment_parts.append(
                    f"- {pred.get('symbol', 'N/A')}: {pred.get('direction', 'N/A')} "
                    f"(confidence: {pred.get('confidence', 0):.0%})"
                )
    
    if enrichment_parts:
        return base_context + "\n" + "\n".join(enrichment_parts)
    return base_context


def _parse_regime_response(content: str) -> dict[str, Any]:
    """Parse the agent's JSON response."""
    
    try:
        # Try to extract JSON from the response
        content = content.strip()
        
        # Handle markdown code blocks
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            content = content[start:end].strip()
        elif "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            content = content[start:end].strip()
        
        result = json.loads(content)
        
        # Validate regime value
        valid_regimes = [r.value for r in MarketRegime]
        if result.get("regime") not in valid_regimes:
            result["regime"] = MarketRegime.UNKNOWN.value
        
        # Ensure confidence is in range
        confidence = float(result.get("confidence", 0.5))
        result["confidence"] = max(0.0, min(1.0, confidence))
        
        # Ensure reasoning exists
        if "reasoning" not in result:
            result["reasoning"] = "No reasoning provided"
        
        return result
        
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse regime response as JSON: {e}")
        return {
            "regime": MarketRegime.UNKNOWN.value,
            "confidence": 0.0,
            "reasoning": f"Failed to parse response: {content[:200]}",
        }
