"""
News Analyst Agent

Fetches and analyzes financial news for sentiment scoring.
Uses Google News RSS and Groq LLM for sentiment classification.

Features:
- Fetches news from Google News RSS
- AI-powered sentiment scoring (-1 to +1)
- Supports stock-specific and market-wide news
- TTL caching to reduce API calls
- Rate limiting and circuit breaker for resilience
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import feedparser
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from src.config import get_settings
from src.finops import record_llm_response
from src.utils.cache import get_news_cache, get_sentiment_cache
from src.utils.circuit_breaker import CircuitBreakerOpenError, get_groq_circuit_breaker
from src.utils.rate_limiter import get_groq_limiter

logger = logging.getLogger(__name__)


# Google News RSS base URL
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"


@dataclass
class NewsItem:
    """A single news article."""

    title: str
    source: str
    published: str
    link: str
    sentiment_score: float = 0.0  # -1 to +1

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "published": self.published,
            "link": self.link,
            "sentiment_score": self.sentiment_score,
        }


@dataclass
class NewsSentiment:
    """Aggregated news sentiment for a symbol or topic."""

    query: str
    items: list[NewsItem]
    avg_sentiment: float = 0.0
    sentiment_label: str = "neutral"  # bullish, bearish, neutral
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "avg_sentiment": self.avg_sentiment,
            "sentiment_label": self.sentiment_label,
            "news_count": len(self.items),
            "headlines": [item.title for item in self.items[:5]],
            "timestamp": self.timestamp.isoformat(),
        }


SENTIMENT_SYSTEM_PROMPT = """You are a financial news sentiment analyzer.

Analyze the following news headlines and rate the overall sentiment on a scale from -1.0 to +1.0:
- -1.0 = Extremely bearish (very negative for stock price)
- -0.5 = Bearish
- 0.0 = Neutral
- +0.5 = Bullish
- +1.0 = Extremely bullish (very positive for stock price)

Consider:
- Company earnings, revenue, growth
- Market conditions, regulations
- Management changes, strategic decisions
- Industry trends, competition

Respond with ONLY a JSON object:
{"sentiment": <float between -1.0 and 1.0>, "reasoning": "<brief explanation>"}"""


class NewsAnalyst:
    """
    Fetches and analyzes financial news for sentiment.

    Uses Google News RSS for headlines and Groq LLM for sentiment scoring.
    Implements caching, rate limiting, and circuit breaker for resilience.
    """

    def __init__(self):
        self.settings = get_settings()
        self._llm = None
        self._news_cache = get_news_cache()
        self._sentiment_cache = get_sentiment_cache()
        self._rate_limiter = get_groq_limiter()
        self._circuit_breaker = get_groq_circuit_breaker()

    def _get_llm(self) -> ChatGroq:
        """Get or create LLM instance."""
        if self._llm is None:
            self._llm = ChatGroq(
                api_key=self.settings.groq_api_key.get_secret_value(),
                model_name=self.settings.groq_model_fallback,  # Use smaller model for news
                temperature=0.1,
                max_tokens=256,
            )
        return self._llm

    def fetch_news(self, query: str, max_items: int = 10) -> list[NewsItem]:
        """
        Fetch news from Google News RSS with caching.

        Args:
            query: Search query (e.g., "RELIANCE stock" or "Indian stock market")
            max_items: Maximum number of items to fetch

        Returns:
            List of NewsItem objects
        """
        # Check cache first
        cache_key = f"news:{query}:{max_items}"
        cached = self._news_cache.get(cache_key)
        if cached is not None:
            logger.debug(f"News cache hit for '{query}'")
            return cached

        try:
            url = GOOGLE_NEWS_RSS.format(query=quote(query))
            feed = feedparser.parse(url)

            items = []
            for entry in feed.entries[:max_items]:
                # Extract source from title (Google News format: "Title - Source")
                title = entry.get("title", "")
                source = ""
                if " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title = parts[0]
                    source = parts[1] if len(parts) > 1 else ""

                items.append(
                    NewsItem(
                        title=title,
                        source=source,
                        published=entry.get("published", ""),
                        link=entry.get("link", ""),
                    )
                )

            # Cache the result
            ttl = self.settings.cache_news_ttl
            self._news_cache.set(cache_key, items, ttl)

            logger.info(f"Fetched {len(items)} news items for '{query}'")
            return items

        except Exception as e:
            logger.error(f"Error fetching news: {e}")
            return []

    async def analyze_sentiment(self, headlines: list[str]) -> tuple[float, str]:
        """
        Analyze sentiment of headlines using LLM with caching.

        Args:
            headlines: List of news headlines

        Returns:
            Tuple of (sentiment_score, reasoning)
        """
        if not headlines:
            return 0.0, "No headlines to analyze"

        # Check cache first
        headlines_key = "|".join(sorted(headlines[:10]))
        cache_key = f"sentiment:{hash(headlines_key)}"
        cached = self._sentiment_cache.get(cache_key)
        if cached is not None:
            logger.debug("Sentiment cache hit")
            return cached

        try:
            # Check circuit breaker
            if not self._circuit_breaker.is_available:
                raise CircuitBreakerOpenError("groq_api", self._circuit_breaker.recovery_time)

            # Apply rate limiting
            if self.settings.enable_rate_limiting:
                self._rate_limiter.acquire_sync()

            llm = self._get_llm()

            # Format headlines for analysis
            headlines_text = "\n".join([f"- {h}" for h in headlines[:10]])

            messages = [
                SystemMessage(content=SENTIMENT_SYSTEM_PROMPT),
                HumanMessage(content=f"Analyze these headlines:\n\n{headlines_text}"),
            ]

            def invoke_llm():
                return llm.invoke(messages)

            response = self._circuit_breaker.call(invoke_llm)
            record_llm_response("news_analyst", response, model=self.settings.groq_model_fallback)
            content = response.content.strip()

            # Parse JSON response
            import json

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
            sentiment = float(result.get("sentiment", 0.0))
            reasoning = result.get("reasoning", "")

            # Clamp to valid range
            sentiment = max(-1.0, min(1.0, sentiment))

            # Cache the result
            ttl = self.settings.cache_sentiment_ttl
            self._sentiment_cache.set(cache_key, (sentiment, reasoning), ttl)

            return sentiment, reasoning

        except CircuitBreakerOpenError as e:
            logger.warning(f"Circuit breaker open for sentiment: {e}")
            return 0.0, "Circuit breaker open - using neutral sentiment"

        except Exception as e:
            logger.error(f"Error analyzing sentiment: {e}")
            return 0.0, f"Analysis failed: {str(e)}"

    async def get_sentiment(
        self,
        query: str,
        max_items: int = 10,
    ) -> NewsSentiment:
        """
        Fetch news and analyze sentiment for a query.

        Args:
            query: Search query
            max_items: Maximum news items to fetch

        Returns:
            NewsSentiment object with aggregated results
        """
        items = self.fetch_news(query, max_items)

        if not items:
            return NewsSentiment(
                query=query,
                items=[],
                avg_sentiment=0.0,
                sentiment_label="neutral",
            )

        # Get headlines for analysis
        headlines = [item.title for item in items]

        # Analyze sentiment
        sentiment, reasoning = await self.analyze_sentiment(headlines)

        # Update items with sentiment
        for item in items:
            item.sentiment_score = sentiment

        # Determine label
        if sentiment >= 0.3:
            label = "bullish"
        elif sentiment <= -0.3:
            label = "bearish"
        else:
            label = "neutral"

        logger.info(f"Sentiment for '{query}': {sentiment:.2f} ({label})")

        return NewsSentiment(
            query=query,
            items=items,
            avg_sentiment=sentiment,
            sentiment_label=label,
        )

    async def get_market_sentiment(self) -> NewsSentiment:
        """Get overall Indian stock market sentiment."""
        return await self.get_sentiment("Indian stock market NIFTY SENSEX")

    async def get_stock_sentiment(self, symbol: str) -> NewsSentiment:
        """Get sentiment for a specific stock."""
        query = f"{symbol} stock NSE India"
        return await self.get_sentiment(query)


async def test_news_analyst():
    """Test the news analyst."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[NEWS] RakshaQuant - News Analyst Test")
    print("=" * 60)

    analyst = NewsAnalyst()

    # Test market sentiment
    print("\n[MARKET] Fetching Indian market news...")
    market_sentiment = await analyst.get_market_sentiment()

    print(
        f"\n  Sentiment: {market_sentiment.avg_sentiment:+.2f} ({market_sentiment.sentiment_label})"
    )
    print(f"  Headlines analyzed: {len(market_sentiment.items)}")

    for item in market_sentiment.items[:3]:
        print(f"    - {item.title[:60]}...")

    # Test stock sentiment
    print("\n[STOCK] Fetching RELIANCE news...")
    stock_sentiment = await analyst.get_stock_sentiment("RELIANCE")

    print(
        f"\n  Sentiment: {stock_sentiment.avg_sentiment:+.2f} ({stock_sentiment.sentiment_label})"
    )
    print(f"  Headlines analyzed: {len(stock_sentiment.items)}")

    for item in stock_sentiment.items[:3]:
        print(f"    - {item.title[:60]}...")

    print("\n" + "=" * 60)
    print("[SUCCESS] News analyst working!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_news_analyst())
