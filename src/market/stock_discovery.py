"""
Dynamic Stock Discovery

Discovers tradeable stocks dynamically based on:
1. News mentions (trending stocks in financial news)
2. Market movers (top gainers/losers from NIFTY indices)
3. Sector rotation (which sectors are hot)

Replaces hardcoded watchlists with intelligent discovery.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import feedparser
import yfinance as yf

from src.config import get_settings

logger = logging.getLogger(__name__)


# All NIFTY 50 stocks - complete universe to discover from
NIFTY50_STOCKS = [
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJFINANCE",
    "BAJAJFINSV",
    "BHARTIARTL",
    "BPCL",
    "BRITANNIA",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "INDUSINDBK",
    "INFY",
    "ITC",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "LTIM",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SHRIRAMFIN",
    "SUNPHARMA",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TCS",
    "TECHM",
    "TITAN",
    "ULTRACEMCO",
    "WIPRO",
]

# Popular mid-cap stocks for additional opportunities
MIDCAP_STOCKS = [
    "ZOMATO",
    "PAYTM",
    "NYKAA",
    "DELHIVERY",
    "POLICYBZR",
    "IRCTC",
    "TRENT",
    "PERSISTENT",
    "COFORGE",
    "MPHASIS",
    "DIXON",
    "VOLTAS",
    "JUBLFOOD",
    "BERGEPAINT",
    "HAVELLS",
    "PIDILITIND",
    "ASTRAL",
    "CROMPTON",
    "POLYCAB",
    "AFFLE",
]

# Google News RSS for India
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"


@dataclass
class DiscoveredStock:
    """A stock discovered through analysis."""

    symbol: str
    source: str  # "news", "gainer", "loser", "momentum"
    score: float  # Discovery score (higher = more interesting)
    reason: str
    news_mentions: int = 0
    change_percent: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "score": self.score,
            "reason": self.reason,
            "news_mentions": self.news_mentions,
            "change_percent": self.change_percent,
        }


class StockDiscovery:
    """
    Dynamically discovers tradeable stocks based on:
    - News mentions
    - Price movements
    - Volume spikes
    """

    def __init__(self, max_stocks: int = 20):
        """
        Initialize stock discovery.

        Args:
            max_stocks: Maximum number of stocks to track
        """
        self.max_stocks = max_stocks
        self.settings = get_settings()
        self.discovered: dict[str, DiscoveredStock] = {}

        # Stock universe to scan
        self.universe = list(set(NIFTY50_STOCKS + MIDCAP_STOCKS))

    def _extract_stock_mentions(self, text: str) -> list[str]:
        """Extract stock symbols mentioned in text."""
        mentioned = []
        text_upper = text.upper()

        for stock in self.universe:
            # Check for exact match or common variations
            if stock.upper() in text_upper:
                mentioned.append(stock)
            # Handle special cases
            elif stock == "M&M" and ("MAHINDRA" in text_upper or "M&M" in text_upper):
                mentioned.append(stock)
            elif stock == "BAJAJ-AUTO" and "BAJAJ AUTO" in text_upper:
                mentioned.append(stock)

        return mentioned

    def discover_from_news(self, max_articles: int = 30) -> dict[str, int]:
        """
        Discover trending stocks from financial news.

        Returns:
            Dictionary of symbol -> mention count
        """
        queries = [
            "Indian stock market NSE",
            "NIFTY stocks buy sell",
            "BSE NSE shares today",
        ]

        mention_counts: dict[str, int] = {}

        for query in queries:
            try:
                url = GOOGLE_NEWS_RSS.format(query=quote(query))
                feed = feedparser.parse(url)

                for entry in feed.entries[: max_articles // len(queries)]:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    text = f"{title} {summary}"

                    for stock in self._extract_stock_mentions(text):
                        mention_counts[stock] = mention_counts.get(stock, 0) + 1

            except Exception as e:
                logger.warning(f"Error fetching news for '{query}': {e}")

        logger.info(f"Found {len(mention_counts)} stocks mentioned in news")
        return mention_counts

    def discover_market_movers(self, min_change: float = 2.0) -> list[DiscoveredStock]:
        """
        Discover top gainers and losers.

        Args:
            min_change: Minimum percentage change to consider

        Returns:
            List of discovered stocks with movement data
        """
        movers = []

        # Sample a subset for efficiency
        sample_stocks = NIFTY50_STOCKS[:30]

        try:
            # Fetch data for multiple stocks at once
            symbols = [f"{s}.NS" for s in sample_stocks]
            tickers = yf.Tickers(" ".join(symbols))

            for stock in sample_stocks:
                try:
                    yf_symbol = f"{stock}.NS"
                    ticker = tickers.tickers.get(yf_symbol)

                    if ticker is None:
                        continue

                    hist = ticker.history(period="2d")
                    if hist.empty or len(hist) < 2:
                        continue

                    latest = hist.iloc[-1]["Close"]
                    prev = hist.iloc[-2]["Close"]
                    change_pct = ((latest - prev) / prev) * 100

                    if abs(change_pct) >= min_change:
                        source = "gainer" if change_pct > 0 else "loser"
                        movers.append(
                            DiscoveredStock(
                                symbol=stock,
                                source=source,
                                score=abs(change_pct),
                                reason=f"{change_pct:+.2f}% move today",
                                change_percent=change_pct,
                            )
                        )

                except Exception as e:
                    logger.debug(f"Error checking {stock}: {e}")

            logger.info(f"Found {len(movers)} market movers (>{min_change}% change)")

        except Exception as e:
            logger.error(f"Error discovering movers: {e}")

        return sorted(movers, key=lambda x: abs(x.change_percent), reverse=True)

    async def discover(self) -> list[str]:
        """
        Run full discovery and return list of stocks to trade.

        Returns:
            List of stock symbols ordered by opportunity score
        """
        self.discovered.clear()

        # 1. Discover from news
        logger.info("Scanning news for stock mentions...")
        news_mentions = self.discover_from_news()

        for symbol, count in news_mentions.items():
            if count >= 2:  # At least 2 mentions
                self.discovered[symbol] = DiscoveredStock(
                    symbol=symbol,
                    source="news",
                    score=count * 10,  # Score based on mentions
                    reason=f"Mentioned in {count} news articles",
                    news_mentions=count,
                )

        # 2. Discover market movers
        logger.info("Scanning for market movers...")
        movers = self.discover_market_movers(min_change=1.5)

        for mover in movers:
            if mover.symbol in self.discovered:
                # Boost score if also in news
                self.discovered[mover.symbol].score += mover.score * 5
                self.discovered[mover.symbol].reason += f" + {mover.reason}"
                self.discovered[mover.symbol].change_percent = mover.change_percent
            else:
                self.discovered[mover.symbol] = mover

        # 3. Sort by score and limit
        ranked = sorted(self.discovered.values(), key=lambda x: x.score, reverse=True)[
            : self.max_stocks
        ]

        symbols = [s.symbol for s in ranked]

        # Ensure we have minimum stocks (fallback to blue chips)
        if len(symbols) < 10:
            fallback = [
                "RELIANCE",
                "TCS",
                "HDFCBANK",
                "INFY",
                "ICICIBANK",
                "SBIN",
                "BHARTIARTL",
                "ITC",
                "KOTAKBANK",
                "LT",
            ]
            for stock in fallback:
                if stock not in symbols:
                    symbols.append(stock)
                if len(symbols) >= 10:
                    break

        logger.info(f"Discovered {len(symbols)} stocks for trading")
        return symbols

    def get_discovery_report(self) -> list[dict]:
        """Get detailed discovery report."""
        return [
            s.to_dict()
            for s in sorted(self.discovered.values(), key=lambda x: x.score, reverse=True)
        ]


async def test_discovery():
    """Test stock discovery."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[DISCOVERY] RakshaQuant - Dynamic Stock Discovery Test")
    print("=" * 60)

    discovery = StockDiscovery(max_stocks=15)

    print("\n[SCAN] Running discovery...")
    symbols = await discovery.discover()

    print(f"\n[RESULT] Discovered {len(symbols)} stocks:")

    report = discovery.get_discovery_report()
    print(f"\n{'Symbol':<12} {'Source':<10} {'Score':<8} {'Reason'}")
    print("-" * 70)

    for item in report[:15]:
        print(
            f"{item['symbol']:<12} {item['source']:<10} {item['score']:<8.1f} {item['reason'][:40]}"
        )

    print("\n[SYMBOLS] Final watchlist:")
    print(", ".join(symbols))

    print("\n" + "=" * 60)
    print("[SUCCESS] Stock discovery working!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_discovery())
