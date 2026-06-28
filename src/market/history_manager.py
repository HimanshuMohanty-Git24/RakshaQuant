"""
Historical Data Manager

Pre-fetches, caches, and maintains rolling OHLCV DataFrames for each symbol.
Enables real indicator calculation from actual price history instead of
fabricating indicators from a single price point.

Features:
- Pre-fetch historical data on startup via YFinance
- Append new quotes as intraday candles
- Configurable lookback and cache TTL
- Thread-safe data access
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from src.market.yfinance_feed import YFinanceFeed
from src.utils.market_time import now_ist

logger = logging.getLogger(__name__)

# Minimum bars needed for indicator calculation
MIN_BARS_FOR_INDICATORS = 50
DEFAULT_LOOKBACK_PERIOD = "3mo"  # 3 months of daily data
MAX_INTRADAY_BARS = 500  # Max intraday bars to keep in memory


class HistoryManager:
    """
    Manages rolling historical DataFrames for each symbol.

    Pre-fetches daily OHLCV data on startup and merges new intraday
    quotes to maintain a continuously updated price history suitable
    for real indicator calculation.
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        lookback_period: str = DEFAULT_LOOKBACK_PERIOD,
    ):
        """
        Initialize the history manager.

        Args:
            symbols: List of stock symbols to track
            lookback_period: YFinance period string for initial data fetch
        """
        self.symbols = symbols or []
        self.lookback_period = lookback_period

        # Thread-safe storage for historical DataFrames
        self._lock = threading.Lock()
        self._history: dict[str, pd.DataFrame] = {}
        self._last_fetch: dict[str, datetime] = {}
        self._feed = YFinanceFeed(symbols=self.symbols)

    def prefetch_all(self) -> dict[str, bool]:
        """
        Pre-fetch historical data for all symbols.

        Returns:
            Dict of symbol -> success status
        """
        results = {}
        logger.info(f"Pre-fetching historical data for {len(self.symbols)} symbols...")

        for symbol in self.symbols:
            success = self.fetch_history(symbol)
            results[symbol] = success

        fetched = sum(1 for v in results.values() if v)
        logger.info(f"Pre-fetch complete: {fetched}/{len(self.symbols)} symbols loaded")
        return results

    def fetch_history(self, symbol: str, period: str | None = None) -> bool:
        """
        Fetch historical data for a single symbol.

        Args:
            symbol: Stock symbol (e.g., 'RELIANCE')
            period: YFinance period string (default: lookback_period)

        Returns:
            True if data was fetched successfully
        """
        period = period or self.lookback_period
        try:
            df = self._feed.get_historical(symbol, period=period)

            if df is None or df.empty:
                logger.warning(f"No historical data returned for {symbol}")
                return False

            # Normalize column names to lowercase for consistency
            df.columns = [c.lower() for c in df.columns]

            # Ensure required columns exist
            required = ["open", "high", "low", "close", "volume"]
            if not all(col in df.columns for col in required):
                logger.warning(f"Missing columns for {symbol}: {df.columns.tolist()}")
                return False

            with self._lock:
                self._history[symbol] = df.copy()
                self._last_fetch[symbol] = datetime.now()

            logger.info(
                f"Loaded {len(df)} bars for {symbol} "
                f"({df.index[0].date()} to {df.index[-1].date()})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to fetch history for {symbol}: {e}")
            return False

    def append_quote(
        self,
        symbol: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        timestamp: datetime | None = None,
    ) -> None:
        """
        Append a new quote as a candle to the symbol's history.

        If the timestamp matches the last bar's date, the bar is updated
        (intraday aggregation). Otherwise, a new bar is appended.

        Args:
            symbol: Stock symbol
            open_price: Open price
            high: High price
            low: Low price
            close: Close price
            volume: Volume
            timestamp: Quote timestamp (default: now, in IST)
        """
        if timestamp is None:
            timestamp = now_ist()

        with self._lock:
            if symbol not in self._history:
                logger.debug(f"No history for {symbol}, skipping quote append")
                return

            df = self._history[symbol]

            # Check if we should update the last bar or create a new one
            if len(df) > 0:
                last_date = df.index[-1]

                # Same trading day — update the last bar
                if hasattr(last_date, "date") and last_date.date() == timestamp.date():
                    df.at[df.index[-1], "high"] = max(df.iloc[-1]["high"], high)
                    df.at[df.index[-1], "low"] = min(df.iloc[-1]["low"], low)
                    df.at[df.index[-1], "close"] = close
                    # Quote volume is the cumulative daily total, not a per-tick delta —
                    # keep the latest (monotonic) cumulative value instead of summing,
                    # which previously inflated volume without bound each cycle.
                    df.at[df.index[-1], "volume"] = max(float(df.iloc[-1]["volume"]), float(volume))
                    return

            # New day or first bar — append new row
            new_row = pd.DataFrame(
                {
                    "open": [open_price],
                    "high": [high],
                    "low": [low],
                    "close": [close],
                    "volume": [volume],
                },
                index=[pd.Timestamp(timestamp)],
            )

            self._history[symbol] = pd.concat([df, new_row])

            # Trim to max bars to prevent memory growth
            if len(self._history[symbol]) > MAX_INTRADAY_BARS:
                self._history[symbol] = self._history[symbol].iloc[-MAX_INTRADAY_BARS:]

    def get_history(
        self,
        symbol: str,
        bars: int | None = None,
        include_forming: bool = True,
    ) -> pd.DataFrame | None:
        """
        Get historical DataFrame for a symbol.

        Args:
            symbol: Stock symbol
            bars: Number of recent bars to return (None = all)
            include_forming: If False, drop the current (still-forming) bar — i.e. any
                trailing rows dated today (IST). The forming bar's OHLC repaints as live
                quotes arrive, so indicators/signals computed on it look ahead within the
                bar; pass False to compute on settled bars only. Never returns empty
                purely from this trim (falls back to the full history if everything is today).

        Returns:
            DataFrame with OHLCV columns, or None if not available
        """
        with self._lock:
            if symbol not in self._history:
                return None

            df = self._history[symbol].copy()

        if not include_forming and len(df) > 0:
            today = now_ist().date()
            settled = df[[not (hasattr(ts, "date") and ts.date() == today) for ts in df.index]]
            if len(settled) > 0:
                df = settled

        if bars is not None and len(df) > bars:
            df = df.iloc[-bars:]

        return df

    def has_sufficient_data(self, symbol: str, min_bars: int = MIN_BARS_FOR_INDICATORS) -> bool:
        """
        Check if a symbol has enough data for indicator calculation.

        Args:
            symbol: Stock symbol
            min_bars: Minimum number of bars required

        Returns:
            True if sufficient data is available
        """
        with self._lock:
            if symbol not in self._history:
                return False
            return len(self._history[symbol]) >= min_bars

    def get_available_symbols(self) -> list[str]:
        """Get list of symbols with loaded history."""
        with self._lock:
            return [s for s in self._history if len(self._history[s]) >= MIN_BARS_FOR_INDICATORS]

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about loaded history data."""
        with self._lock:
            stats = {}
            for symbol, df in self._history.items():
                stats[symbol] = {
                    "bars": len(df),
                    "start": str(df.index[0].date()) if len(df) > 0 else "N/A",
                    "end": str(df.index[-1].date()) if len(df) > 0 else "N/A",
                    "sufficient": len(df) >= MIN_BARS_FOR_INDICATORS,
                }
            return stats

    def refresh_stale(self, max_age_hours: int = 12) -> int:
        """
        Re-fetch data for symbols whose cache is stale.

        Args:
            max_age_hours: Maximum age in hours before refetching

        Returns:
            Number of symbols refreshed
        """
        refreshed = 0
        cutoff = datetime.now() - timedelta(hours=max_age_hours)

        for symbol in self.symbols:
            last = self._last_fetch.get(symbol)
            if last is None or last < cutoff:
                if self.fetch_history(symbol):
                    refreshed += 1

        return refreshed
