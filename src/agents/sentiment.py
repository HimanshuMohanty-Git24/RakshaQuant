"""
Market Sentiment Agent

Calculates a Market Mood Index (0-100) combining multiple signals:
- News sentiment
- Market volatility (VIX-like)
- Market breadth (advance/decline)

Provides Fear/Neutral/Greed classification.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class SentimentSignal:
    """Market sentiment signal."""

    mood_index: int  # 0-100 (0=Extreme Fear, 50=Neutral, 100=Extreme Greed)
    mood_label: str  # extreme_fear, fear, neutral, greed, extreme_greed
    news_score: float  # -1 to +1
    volatility_score: float  # 0-100 (higher = more volatile)
    breadth_score: float  # -1 to +1 (positive = more advancers)
    confidence: float  # 0-1
    reasoning: str
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mood_index": self.mood_index,
            "mood_label": self.mood_label,
            "news_score": self.news_score,
            "volatility_score": self.volatility_score,
            "breadth_score": self.breadth_score,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp.isoformat(),
        }


def calculate_mood_label(mood_index: int) -> str:
    """Convert mood index to label."""
    if mood_index <= 20:
        return "extreme_fear"
    elif mood_index <= 40:
        return "fear"
    elif mood_index <= 60:
        return "neutral"
    elif mood_index <= 80:
        return "greed"
    else:
        return "extreme_greed"


class MarketSentimentAgent:
    """
    Calculates Market Mood Index from multiple signals.

    Components:
    - News Sentiment (35%): AI analysis of news headlines
    - Volatility (35%): Market volatility measurement
    - Market Breadth (30%): Advance/decline ratio
    """

    def __init__(self):
        self.settings = get_settings()

        # Weights for each component
        self.news_weight = 0.35
        self.volatility_weight = 0.35
        self.breadth_weight = 0.30

    def calculate_volatility_score(
        self,
        current_volatility: float,
        avg_volatility: float = 15.0,
    ) -> float:
        """
        Calculate volatility score (0-100).

        High volatility = Fear, Low volatility = Complacency/Greed

        Args:
            current_volatility: Current volatility (e.g., daily % range)
            avg_volatility: Average historical volatility

        Returns:
            Score 0-100 (inverted - high volatility = low score)
        """
        if avg_volatility <= 0:
            return 50.0

        ratio = current_volatility / avg_volatility

        # Map ratio to 0-100 (inverted)
        # ratio > 1.5 = high fear (low score)
        # ratio < 0.5 = low fear (high score)
        if ratio >= 2.0:
            score = 10.0
        elif ratio >= 1.5:
            score = 25.0
        elif ratio >= 1.0:
            score = 45.0
        elif ratio >= 0.75:
            score = 60.0
        elif ratio >= 0.5:
            score = 75.0
        else:
            score = 85.0

        return score

    def calculate_breadth_score(
        self,
        advancers: int,
        decliners: int,
    ) -> float:
        """
        Calculate market breadth score (-1 to +1).

        Args:
            advancers: Number of stocks going up
            decliners: Number of stocks going down

        Returns:
            Score -1 (all down) to +1 (all up)
        """
        total = advancers + decliners
        if total == 0:
            return 0.0

        return (advancers - decliners) / total

    def calculate_mood_index(
        self,
        news_sentiment: float,  # -1 to +1
        volatility_score: float,  # 0-100
        breadth_score: float,  # -1 to +1
    ) -> int:
        """
        Calculate overall Market Mood Index (0-100).

        Args:
            news_sentiment: News sentiment score (-1 to +1)
            volatility_score: Volatility score (0-100, higher = less volatile)
            breadth_score: Market breadth (-1 to +1)

        Returns:
            Mood index 0-100
        """
        # Normalize news sentiment to 0-100
        news_normalized = (news_sentiment + 1) * 50  # -1->0, 0->50, 1->100

        # Normalize breadth to 0-100
        breadth_normalized = (breadth_score + 1) * 50  # -1->0, 0->50, 1->100

        # Weighted average
        mood_index = (
            news_normalized * self.news_weight
            + volatility_score * self.volatility_weight
            + breadth_normalized * self.breadth_weight
        )

        return int(max(0, min(100, mood_index)))

    def analyze(
        self,
        news_sentiment: float = 0.0,
        market_data: dict[str, Any] | None = None,
        volatility: float | None = None,
    ) -> SentimentSignal:
        """
        Analyze market sentiment and return signal.

        Args:
            news_sentiment: Pre-calculated news sentiment (-1 to +1)
            market_data: Dictionary of symbol -> price data
            volatility: Pre-calculated volatility (optional)

        Returns:
            SentimentSignal with mood index and components
        """
        # Calculate breadth from market data
        advancers = 0
        decliners = 0
        avg_change = 0.0

        if market_data:
            for symbol, data in market_data.items():
                if isinstance(data, dict):
                    change = data.get("change_percent", 0)
                    avg_change += abs(change)
                    if change > 0:
                        advancers += 1
                    elif change < 0:
                        decliners += 1

            if market_data:
                avg_change /= len(market_data)

        breadth_score = self.calculate_breadth_score(advancers, decliners)

        # Calculate volatility score
        if volatility is not None:
            vol_score = self.calculate_volatility_score(volatility)
        else:
            # Estimate from average change
            vol_score = self.calculate_volatility_score(avg_change, avg_volatility=1.0)

        # Calculate mood index
        mood_index = self.calculate_mood_index(
            news_sentiment=news_sentiment,
            volatility_score=vol_score,
            breadth_score=breadth_score,
        )

        mood_label = calculate_mood_label(mood_index)

        # Determine confidence based on data availability
        confidence = 0.7
        if not market_data:
            confidence -= 0.2
        if volatility is None:
            confidence -= 0.1

        # Build reasoning
        reasoning_parts = []
        if news_sentiment >= 0.3:
            reasoning_parts.append("positive news sentiment")
        elif news_sentiment <= -0.3:
            reasoning_parts.append("negative news sentiment")

        if breadth_score >= 0.3:
            reasoning_parts.append(
                f"strong breadth ({advancers}/{advancers + decliners} advancers)"
            )
        elif breadth_score <= -0.3:
            reasoning_parts.append(f"weak breadth ({advancers}/{advancers + decliners} advancers)")

        if vol_score >= 70:
            reasoning_parts.append("low volatility")
        elif vol_score <= 30:
            reasoning_parts.append("high volatility")

        reasoning = (
            f"Market mood is {mood_label}. " + ", ".join(reasoning_parts)
            if reasoning_parts
            else f"Market mood is {mood_label}."
        )

        logger.info(f"Market Sentiment: {mood_index}/100 ({mood_label})")

        return SentimentSignal(
            mood_index=mood_index,
            mood_label=mood_label,
            news_score=news_sentiment,
            volatility_score=vol_score,
            breadth_score=breadth_score,
            confidence=confidence,
            reasoning=reasoning,
        )


def sentiment_analysis_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node for sentiment analysis.

    Injects market_mood into state for use by other agents.
    """
    agent = MarketSentimentAgent()

    # Get news sentiment if available
    news_sentiment_data = state.get("news_sentiment", {})
    news_score = news_sentiment_data.get("avg_sentiment", 0.0)

    # Get market data
    market_data = state.get("market_data", {})

    # Analyze
    signal = agent.analyze(
        news_sentiment=news_score,
        market_data=market_data,
    )

    return {
        "market_mood": signal.to_dict(),
    }


def test_sentiment_agent():
    """Test the sentiment agent."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[SENTIMENT] RakshaQuant - Market Sentiment Test")
    print("=" * 60)

    agent = MarketSentimentAgent()

    # Simulate market data
    market_data = {
        "RELIANCE": {"change_percent": 1.5},
        "TCS": {"change_percent": -0.5},
        "HDFCBANK": {"change_percent": 0.8},
        "INFY": {"change_percent": 1.2},
        "SBIN": {"change_percent": -0.3},
    }

    print("\n[DATA] Simulated market data:")
    for symbol, data in market_data.items():
        print(f"  {symbol}: {data['change_percent']:+.2f}%")

    print("\n[ANALYSIS] Calculating sentiment...")

    # Test with different news sentiments
    for news_score in [-0.5, 0.0, 0.5]:
        signal = agent.analyze(
            news_sentiment=news_score,
            market_data=market_data,
        )

        print(f"\n  News Sentiment: {news_score:+.2f}")
        print(f"  Mood Index: {signal.mood_index}/100")
        print(f"  Mood Label: {signal.mood_label}")
        print(f"  Reasoning: {signal.reasoning}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Sentiment agent working!")
    print("=" * 60)


if __name__ == "__main__":
    test_sentiment_agent()
