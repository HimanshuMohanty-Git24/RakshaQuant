"""
Simulated Market Data Module

Provides realistic simulated market data for paper trading testing.
Uses real NSE stock prices as base with random fluctuations.

NOTE: DhanHQ Sandbox doesn't support live market feed API.
      For live data, you need a production token from web.dhan.co
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


# Base prices for NSE stocks (approximate real values as of Jan 2026)
NSE_BASE_PRICES = {
    "RELIANCE": 2450.0,
    "TCS": 4200.0,
    "HDFCBANK": 1680.0,
    "INFY": 1850.0,
    "ICICIBANK": 1280.0,
    "HINDUNILVR": 2350.0,
    "SBIN": 780.0,
    "BHARTIARTL": 1620.0,
    "ITC": 465.0,
    "KOTAKBANK": 1820.0,
    "LT": 3650.0,
    "AXISBANK": 1150.0,
    "ASIANPAINT": 2280.0,
    "MARUTI": 11200.0,
    "TITAN": 3450.0,
    "BAJFINANCE": 7200.0,
    "WIPRO": 295.0,
    "ULTRACEMCO": 11500.0,
    "NESTLEIND": 2180.0,
    "TECHM": 1680.0,
}

# Security IDs for NSE stocks
NSE_SECURITY_IDS = {
    "RELIANCE": 2885,
    "TCS": 11536,
    "HDFCBANK": 1333,
    "INFY": 1594,
    "ICICIBANK": 4963,
    "HINDUNILVR": 1394,
    "SBIN": 3045,
    "BHARTIARTL": 10604,
    "ITC": 1660,
    "KOTAKBANK": 1922,
    "LT": 11483,
    "AXISBANK": 5900,
    "ASIANPAINT": 236,
    "MARUTI": 10999,
    "TITAN": 3506,
    "BAJFINANCE": 317,
    "WIPRO": 3787,
    "ULTRACEMCO": 11532,
    "NESTLEIND": 17963,
    "TECHM": 13538,
}


@dataclass
class SimulatedQuote:
    """Simulated market quote for a stock."""

    symbol: str
    security_id: int
    last_price: float
    open: float
    high: float
    low: float
    close: float  # Previous close
    change: float
    change_percent: float
    volume: int
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_bullish(self) -> bool:
        return self.change_percent > 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "last_price": self.last_price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "change": self.change,
            "change_percent": self.change_percent,
            "volume": self.volume,
        }


class SimulatedMarketData:
    """
    Provides simulated market data for paper trading testing.

    This class generates realistic price movements based on:
    - Base prices for major NSE stocks
    - Random daily fluctuations
    - Trending behavior
    """

    def __init__(self, volatility: float = 0.02):
        """
        Initialize simulated market data.

        Args:
            volatility: Daily volatility factor (default 2%)
        """
        self.volatility = volatility
        self.base_prices = NSE_BASE_PRICES.copy()
        self.current_prices = NSE_BASE_PRICES.copy()
        self.trends = {
            symbol: random.choice([-1, 1]) * random.uniform(0.001, 0.005)
            for symbol in self.base_prices
        }
        self._initialized = False

    def _simulate_day(self):
        """Simulate a new trading day."""
        for symbol in self.current_prices:
            # Apply trend with some randomness
            trend = self.trends[symbol]
            random_factor = random.uniform(-self.volatility, self.volatility)
            change_pct = trend + random_factor

            # Apply change
            self.current_prices[symbol] *= 1 + change_pct

            # Occasionally reverse trend
            if random.random() < 0.1:
                self.trends[symbol] = -self.trends[symbol]

        self._initialized = True

    def get_quotes(self, symbols: list[str] | None = None) -> dict[str, SimulatedQuote]:
        """
        Get simulated quotes for specified symbols.

        Args:
            symbols: List of symbols, or None for all

        Returns:
            Dictionary of symbol -> SimulatedQuote
        """
        if not self._initialized:
            self._simulate_day()

        if symbols is None:
            symbols = list(self.base_prices.keys())

        quotes = {}

        for symbol in symbols:
            if symbol not in self.current_prices:
                continue

            base = self.base_prices[symbol]
            current = self.current_prices[symbol]

            # Simulate intraday OHLC
            daily_range = current * self.volatility
            open_price = current + random.uniform(-daily_range / 4, daily_range / 4)
            high = max(current, open_price) + random.uniform(0, daily_range / 2)
            low = min(current, open_price) - random.uniform(0, daily_range / 2)

            # Previous close (for change calculation)
            prev_close = base * (1 + random.uniform(-0.01, 0.01))

            change = current - prev_close
            change_pct = (change / prev_close) * 100

            quotes[symbol] = SimulatedQuote(
                symbol=symbol,
                security_id=NSE_SECURITY_IDS.get(symbol, 0),
                last_price=round(current, 2),
                open=round(open_price, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(prev_close, 2),
                change=round(change, 2),
                change_percent=round(change_pct, 2),
                volume=random.randint(100000, 5000000),
            )

        logger.info(f"Generated {len(quotes)} simulated quotes")
        return quotes

    def tick(self):
        """
        Simulate a price tick (small random movement).

        Call this periodically to simulate live price changes.
        """
        for symbol in self.current_prices:
            # Small random movement
            tick_change = random.uniform(-0.002, 0.002)
            self.current_prices[symbol] *= 1 + tick_change

    def get_trading_candidates(self, min_change: float = 0.5) -> list[SimulatedQuote]:
        """
        Get stocks with significant movement for trading.

        Args:
            min_change: Minimum absolute % change to qualify

        Returns:
            List of quotes sorted by change magnitude
        """
        quotes = self.get_quotes()

        candidates = [q for q in quotes.values() if abs(q.change_percent) >= min_change]

        # Sort by absolute change
        candidates.sort(key=lambda q: abs(q.change_percent), reverse=True)

        return candidates

    def get_top_movers(self, n: int = 5) -> tuple[list[SimulatedQuote], list[SimulatedQuote]]:
        """
        Get top gainers and losers.

        Returns:
            Tuple of (gainers, losers)
        """
        quotes = self.get_quotes()
        sorted_quotes = sorted(quotes.values(), key=lambda q: q.change_percent, reverse=True)

        gainers = [q for q in sorted_quotes if q.change_percent > 0][:n]
        losers = [q for q in reversed(sorted_quotes) if q.change_percent < 0][:n]

        return gainers, losers


def test_simulated_data():
    """Test the simulated market data."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[SIM] RakshaQuant - Simulated Market Data Test")
    print("=" * 60)

    market = SimulatedMarketData()

    print("\n[TEST] Fetching all stock quotes...")
    quotes = market.get_quotes()

    print(f"\n{'Symbol':<12} {'LTP':>10} {'Change':>10} {'%':>8}")
    print("-" * 45)

    for symbol, q in sorted(quotes.items(), key=lambda x: x[1].change_percent, reverse=True):
        trend = "UP" if q.is_bullish else "DOWN"
        print(
            f"{symbol:<12} {q.last_price:>10,.2f} {q.change:>+10.2f} {q.change_percent:>+7.2f}%  [{trend}]"
        )

    print("\n[TEST] Top Movers...")
    gainers, losers = market.get_top_movers(3)

    print("\nTop Gainers:")
    for g in gainers:
        print(f"  {g.symbol}: {g.change_percent:+.2f}%")

    print("\nTop Losers:")
    for loser in losers:
        print(f"  {loser.symbol}: {loser.change_percent:+.2f}%")

    print("\n[TEST] Trading Candidates (>0.5% move)...")
    candidates = market.get_trading_candidates(0.5)
    print(f"  Found {len(candidates)} candidates")

    for c in candidates[:5]:
        direction = "BUY signal" if c.is_bullish else "SELL signal"
        print(f"  {c.symbol}: {c.change_percent:+.2f}% -> {direction}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Simulated market data working!")
    print("=" * 60)


if __name__ == "__main__":
    test_simulated_data()
