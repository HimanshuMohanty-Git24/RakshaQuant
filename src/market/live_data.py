"""
Live Market Data Module

Fetches real-time market data from DhanHQ API for NSE stocks.
"""

import logging
from dataclasses import dataclass

import requests

from src.config import get_settings

logger = logging.getLogger(__name__)


# NSE Stock Security IDs (from DhanHQ instrument list)
# These are popular NSE stocks for trading
NSE_WATCHLIST = {
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
class LiveQuote:
    """Live market quote for a stock."""

    symbol: str
    security_id: int
    last_price: float
    open: float
    high: float
    low: float
    close: float  # Previous close
    change: float
    change_percent: float

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
        }


class LiveMarketData:
    """
    Fetches live market data from DhanHQ API.
    """

    def __init__(self, watchlist: dict[str, int] | None = None):
        self.settings = get_settings()
        self.base_url = self.settings.dhan_base_url
        self.watchlist = watchlist or NSE_WATCHLIST

        self.headers = {
            "access-token": self.settings.dhan_access_token.get_secret_value(),
            "client-id": self.settings.dhan_client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def get_quotes(self, symbols: list[str] | None = None) -> dict[str, LiveQuote]:
        """
        Fetch live quotes for specified symbols or entire watchlist.

        Args:
            symbols: List of symbols to fetch, or None for all watchlist

        Returns:
            Dictionary of symbol -> LiveQuote
        """
        if symbols is None:
            symbols = list(self.watchlist.keys())

        # Build security ID list for NSE_EQ
        security_ids = []
        symbol_map = {}

        for symbol in symbols:
            if symbol in self.watchlist:
                sec_id = self.watchlist[symbol]
                security_ids.append(sec_id)
                symbol_map[str(sec_id)] = symbol

        if not security_ids:
            logger.warning("No valid symbols to fetch")
            return {}

        # API request
        url = f"{self.base_url}/marketfeed/ohlc"
        payload = {"NSE_EQ": security_ids}

        try:
            response = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            data = response.json()

            if data.get("status") != "success":
                logger.error(f"API error: {data.get('remarks', data)}")
                return {}

            quotes = {}
            nse_data = data.get("data", {}).get("NSE_EQ", {})

            for sec_id_str, quote_data in nse_data.items():
                symbol = symbol_map.get(sec_id_str)
                if symbol:
                    last_price = quote_data.get("last_price", 0)
                    ohlc = quote_data.get("ohlc", {})
                    prev_close = ohlc.get("close", last_price)

                    change = last_price - prev_close if prev_close else 0
                    change_pct = (change / prev_close * 100) if prev_close else 0

                    quotes[symbol] = LiveQuote(
                        symbol=symbol,
                        security_id=int(sec_id_str),
                        last_price=last_price,
                        open=ohlc.get("open", last_price),
                        high=ohlc.get("high", last_price),
                        low=ohlc.get("low", last_price),
                        close=prev_close,
                        change=change,
                        change_percent=change_pct,
                    )

            logger.info(f"Fetched {len(quotes)} live quotes")
            return quotes

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error fetching quotes: {e}")
            return {}

    def get_top_movers(self, n: int = 5) -> tuple[list[LiveQuote], list[LiveQuote]]:
        """
        Get top gainers and losers from watchlist.

        Returns:
            Tuple of (top_gainers, top_losers)
        """
        quotes = self.get_quotes()

        if not quotes:
            return [], []

        sorted_quotes = sorted(
            quotes.values(),
            key=lambda q: q.change_percent,
            reverse=True,
        )

        gainers = [q for q in sorted_quotes if q.change_percent > 0][:n]
        losers = [q for q in reversed(sorted_quotes) if q.change_percent < 0][:n]

        return gainers, losers

    def get_trading_candidates(self) -> list[LiveQuote]:
        """
        Get stocks suitable for trading based on momentum.

        Returns:
            List of stocks with significant price movement
        """
        quotes = self.get_quotes()

        candidates = []
        for quote in quotes.values():
            # Look for significant movers (>0.5% change)
            if abs(quote.change_percent) > 0.5:
                candidates.append(quote)

        # Sort by absolute change percent
        candidates.sort(key=lambda q: abs(q.change_percent), reverse=True)

        return candidates[:10]  # Top 10 candidates


def test_live_data():
    """Test the live market data module."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[LIVE] RakshaQuant - Live Market Data Test")
    print("=" * 60)

    market_data = LiveMarketData()

    # Test single stock
    print("\n[TEST] Fetching RELIANCE quote...")
    quotes = market_data.get_quotes(["RELIANCE"])

    if quotes:
        q = quotes["RELIANCE"]
        print(f"   Symbol: {q.symbol}")
        print(f"   LTP: Rs. {q.last_price:,.2f}")
        print(f"   Change: {q.change:+.2f} ({q.change_percent:+.2f}%)")
        print(f"   O/H/L/C: {q.open}/{q.high}/{q.low}/{q.close}")
    else:
        print("   [WARN] Could not fetch quote")

    # Test multiple stocks
    print("\n[TEST] Fetching top 5 stocks...")
    quotes = market_data.get_quotes(["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN"])

    for symbol, q in quotes.items():
        trend = "[UP]" if q.is_bullish else "[DOWN]"
        print(f"   {symbol:12} Rs.{q.last_price:>8,.2f}  {q.change_percent:>+6.2f}% {trend}")

    # Test trading candidates
    print("\n[TEST] Finding trading candidates...")
    candidates = market_data.get_trading_candidates()

    print(f"   Found {len(candidates)} candidates:")
    for c in candidates[:5]:
        print(f"   - {c.symbol}: {c.change_percent:+.2f}%")

    print("\n" + "=" * 60)
    print("[SUCCESS] Live market data working!")
    print("=" * 60)


if __name__ == "__main__":
    test_live_data()
