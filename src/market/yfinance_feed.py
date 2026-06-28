"""
Yahoo Finance Market Data Feed

Provides free near real-time market data for NSE stocks via Yahoo Finance.
Polls yfinance periodically to get OHLCV data.

Note: Data is typically delayed 1-15 minutes. Suitable for swing trading.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


# NSE Stock symbols - map to Yahoo Finance format (.NS suffix)
NSE_SYMBOLS = {
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "SBIN": "SBIN.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "ITC": "ITC.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "LT": "LT.NS",
    "AXISBANK": "AXISBANK.NS",
    "ASIANPAINT": "ASIANPAINT.NS",
    "MARUTI": "MARUTI.NS",
    "TITAN": "TITAN.NS",
    "BAJFINANCE": "BAJFINANCE.NS",
    "WIPRO": "WIPRO.NS",
    "ULTRACEMCO": "ULTRACEMCO.NS",
    "NESTLEIND": "NESTLEIND.NS",
    "TECHM": "TECHM.NS",
}

# Index for sentiment calculation
NIFTY50_SYMBOL = "^NSEI"


@dataclass
class YFinanceQuote:
    """Quote data from Yahoo Finance."""

    symbol: str
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "change": self.change,
            "change_percent": self.change_percent,
            "volume": self.volume,
            "is_live": False,  # YFinance data is delayed
        }


class YFinanceFeed:
    """
    Yahoo Finance market data feed for NSE stocks.

    Provides:
    - Current quotes for NSE stocks
    - Historical OHLCV data
    - NIFTY50 index data for sentiment
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        poll_interval: int = 60,
        on_quote: Callable[[YFinanceQuote], None] | None = None,
    ):
        """
        Initialize Yahoo Finance feed.

        Args:
            symbols: List of NSE symbols to track (e.g., ["RELIANCE", "TCS"])
            poll_interval: Seconds between data refreshes
            on_quote: Callback for each quote update
        """
        self.symbols = symbols or list(NSE_SYMBOLS.keys())
        self.poll_interval = poll_interval
        self.on_quote = on_quote

        self.quotes: dict[str, YFinanceQuote] = {}
        self.running = False
        self._last_fetch = None

    def _get_yf_symbol(self, symbol: str) -> str:
        """Convert NSE symbol to Yahoo Finance format."""
        # Works with ANY NSE symbol - just add .NS suffix
        return NSE_SYMBOLS.get(symbol, f"{symbol}.NS")

    def fetch_quotes(self) -> dict[str, YFinanceQuote]:
        """
        Fetch current quotes for all tracked symbols.

        Returns:
            Dictionary of symbol -> YFinanceQuote
        """
        yf_symbols = [self._get_yf_symbol(s) for s in self.symbols]

        try:
            # Fetch data for all symbols at once (efficient)
            tickers = yf.Tickers(" ".join(yf_symbols))

            for symbol in self.symbols:
                yf_symbol = self._get_yf_symbol(symbol)

                try:
                    ticker = tickers.tickers.get(yf_symbol)
                    if ticker is None:
                        continue

                    # Get fast info (cached, quick)
                    info = ticker.fast_info

                    # Get historical data for OHLC (last 2 days)
                    hist = ticker.history(period="2d")

                    if hist.empty:
                        logger.warning(f"No data for {symbol}")
                        continue

                    # Latest data
                    latest = hist.iloc[-1]
                    prev_close = (
                        float(hist.iloc[-2]["Close"]) if len(hist) > 1 else float(latest["Close"])
                    )

                    last_price = (
                        float(info.last_price)
                        if hasattr(info, "last_price")
                        else float(latest["Close"])
                    )
                    change = float(last_price - prev_close)
                    change_pct = float(change / prev_close * 100) if prev_close > 0 else 0.0

                    quote = YFinanceQuote(
                        symbol=symbol,
                        last_price=float(round(last_price, 2)),
                        open=float(round(float(latest["Open"]), 2)),
                        high=float(round(float(latest["High"]), 2)),
                        low=float(round(float(latest["Low"]), 2)),
                        close=float(round(prev_close, 2)),
                        change=float(round(change, 2)),
                        change_percent=float(round(change_pct, 2)),
                        volume=int(latest["Volume"]),
                    )

                    self.quotes[symbol] = quote

                    if self.on_quote:
                        self.on_quote(quote)

                except Exception as e:
                    logger.warning(f"Error fetching {symbol}: {e}")

            self._last_fetch = datetime.now()
            logger.info(f"Fetched {len(self.quotes)} quotes from Yahoo Finance")

        except Exception as e:
            logger.error(f"Error fetching quotes: {e}")

        return self.quotes

    def get_quote(self, symbol: str) -> YFinanceQuote | None:
        """Get the latest quote for a symbol."""
        return self.quotes.get(symbol)

    def get_all_quotes(self) -> dict[str, YFinanceQuote]:
        """Get all current quotes."""
        return self.quotes.copy()

    def get_nifty50(self) -> dict[str, Any] | None:
        """Get NIFTY50 index data for market sentiment."""
        try:
            nifty = yf.Ticker(NIFTY50_SYMBOL)
            hist = nifty.history(period="5d")

            if hist.empty:
                return None

            latest = hist.iloc[-1]
            prev_close = hist.iloc[-2]["Close"] if len(hist) > 1 else latest["Close"]

            return {
                "symbol": "NIFTY50",
                "last_price": float(latest["Close"]),
                "change_percent": ((float(latest["Close"]) - prev_close) / prev_close * 100),
                "high": float(latest["High"]),
                "low": float(latest["Low"]),
                "volume": int(latest["Volume"]),
            }
        except Exception as e:
            logger.error(f"Error fetching NIFTY50: {e}")
            return None

    def get_historical(
        self,
        symbol: str,
        period: str = "1mo",
    ) -> pd.DataFrame | None:
        """
        Get historical OHLCV data for a symbol.

        Args:
            symbol: NSE symbol
            period: yfinance period string (1d, 5d, 1mo, 3mo, 6mo, 1y, etc.)

        Returns:
            DataFrame with OHLCV data
        """
        try:
            yf_symbol = self._get_yf_symbol(symbol)
            ticker = yf.Ticker(yf_symbol)
            return ticker.history(period=period)
        except Exception as e:
            logger.error(f"Error fetching historical data for {symbol}: {e}")
            return None

    async def start(self) -> bool:
        """Start the feed (initial fetch)."""
        self.running = True
        self.fetch_quotes()
        return len(self.quotes) > 0

    async def poll_loop(self):
        """Continuously poll for updates."""
        while self.running:
            await asyncio.sleep(self.poll_interval)
            self.fetch_quotes()

    async def stop(self):
        """Stop the feed."""
        self.running = False


def test_yfinance_feed():
    """Test the Yahoo Finance feed."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[YFINANCE] RakshaQuant - Yahoo Finance Data Test")
    print("=" * 60)

    # Test with top 5 stocks
    feed = YFinanceFeed(symbols=["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN"])

    print("\n[FETCH] Fetching quotes from Yahoo Finance...")
    quotes = feed.fetch_quotes()

    print(f"\n{'Symbol':<12} {'LTP':>10} {'Change':>10} {'%':>8}")
    print("-" * 45)

    for symbol, q in sorted(quotes.items(), key=lambda x: x[1].change_percent, reverse=True):
        trend = "▲" if q.is_bullish else "▼"
        print(
            f"{symbol:<12} {q.last_price:>10,.2f} {q.change:>+10.2f} {q.change_percent:>+7.2f}% {trend}"
        )

    # Test NIFTY50
    print("\n[INDEX] NIFTY50 Data:")
    nifty = feed.get_nifty50()
    if nifty:
        print(f"  NIFTY50: {nifty['last_price']:,.2f} ({nifty['change_percent']:+.2f}%)")

    # Test historical data
    print("\n[HISTORY] RELIANCE last 5 days:")
    hist = feed.get_historical("RELIANCE", period="5d")
    if hist is not None:
        print(hist.tail())

    print("\n" + "=" * 60)
    print("[SUCCESS] Yahoo Finance feed working!")
    print("=" * 60)


if __name__ == "__main__":
    test_yfinance_feed()
