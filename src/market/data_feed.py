"""
Market Data Feed Module

Handles real-time market data ingestion from DhanHQ via WebSocket.
Provides async data streaming with queue-based distribution.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from src.config import get_settings

logger = logging.getLogger(__name__)


class Exchange(Enum):
    """Supported exchanges."""

    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"  # NSE F&O
    MCX = "MCX"


class SubscriptionType(Enum):
    """Market data subscription types."""

    TICKER = "Ticker"
    QUOTE = "Quote"
    DEPTH = "Depth"


@dataclass
class MarketTick:
    """Represents a single market data tick."""

    symbol: str
    exchange: Exchange
    ltp: float  # Last traded price
    ltq: int  # Last traded quantity
    volume: int
    open: float
    high: float
    low: float
    close: float  # Previous close
    change: float
    change_percent: float
    timestamp: datetime
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0


@dataclass
class MarketDataFeed:
    """
    Real-time market data feed from DhanHQ.

    Uses WebSocket for streaming data with automatic reconnection.
    Distributes data to subscribers via async queues.
    """

    # WebSocket configuration
    ws_url: str = "wss://api-feed.dhan.co"
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10

    # Internal state
    _ws: Any = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)
    _subscribed_symbols: set[str] = field(default_factory=set, repr=False)
    _callbacks: list[Callable[[MarketTick], None]] = field(default_factory=list, repr=False)

    async def connect(self) -> None:
        """Establish WebSocket connection to DhanHQ."""
        settings = get_settings()

        headers = {
            "Authorization": f"Bearer {settings.dhan_access_token.get_secret_value()}",
            "Content-Type": "application/json",
        }

        attempt = 0
        while attempt < self.max_reconnect_attempts:
            try:
                logger.info(f"Connecting to market data feed (attempt {attempt + 1})...")
                self._ws = await websockets.connect(
                    self.ws_url,
                    extra_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                )
                self._running = True
                logger.info("Connected to market data feed")

                # Re-subscribe to previously subscribed symbols
                if self._subscribed_symbols:
                    await self._resubscribe()

                return

            except Exception as e:
                attempt += 1
                logger.error(f"Connection failed: {e}")
                if attempt < self.max_reconnect_attempts:
                    logger.info(f"Retrying in {self.reconnect_delay}s...")
                    await asyncio.sleep(self.reconnect_delay)

        raise ConnectionError(f"Failed to connect after {self.max_reconnect_attempts} attempts")

    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Disconnected from market data feed")

    async def subscribe(
        self,
        symbols: list[str],
        exchange: Exchange = Exchange.NSE,
        subscription_type: SubscriptionType = SubscriptionType.TICKER,
    ) -> None:
        """
        Subscribe to market data for given symbols.

        Args:
            symbols: List of trading symbols (e.g., ["RELIANCE", "TCS"])
            exchange: Exchange to subscribe on
            subscription_type: Level of data detail
        """
        if not self._ws:
            raise ConnectionError("Not connected to market data feed")

        # Build subscription message
        subscribe_msg = {
            "RequestCode": 15,  # Subscribe request
            "InstrumentCount": len(symbols),
            "InstrumentList": [
                {
                    "ExchangeSegment": exchange.value,
                    "SecurityId": symbol,
                }
                for symbol in symbols
            ],
        }

        await self._ws.send(json.dumps(subscribe_msg))
        self._subscribed_symbols.update(symbols)
        logger.info(f"Subscribed to {len(symbols)} symbols on {exchange.value}")

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from market data for given symbols."""
        if not self._ws:
            return

        unsubscribe_msg = {
            "RequestCode": 16,  # Unsubscribe request
            "InstrumentCount": len(symbols),
            "InstrumentList": [{"SecurityId": symbol} for symbol in symbols],
        }

        await self._ws.send(json.dumps(unsubscribe_msg))
        self._subscribed_symbols -= set(symbols)
        logger.info(f"Unsubscribed from {len(symbols)} symbols")

    def add_subscriber(self, queue: asyncio.Queue) -> None:
        """Add a subscriber queue to receive market data."""
        self._subscribers.append(queue)

    def remove_subscriber(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def add_callback(self, callback: Callable[[MarketTick], None]) -> None:
        """Add a callback function for market data."""
        self._callbacks.append(callback)

    async def start_streaming(self) -> None:
        """Start receiving and distributing market data."""
        if not self._ws:
            await self.connect()

        logger.info("Starting market data stream...")

        while self._running:
            try:
                message = await self._ws.recv()
                tick = self._parse_message(message)

                if tick:
                    # Distribute to subscribers
                    await self._distribute(tick)

            except ConnectionClosed:
                logger.warning("Connection closed, attempting reconnect...")
                await self.connect()
            except Exception as e:
                logger.error(f"Error processing message: {e}")

    def _parse_message(self, message: str | bytes) -> MarketTick | None:
        """Parse incoming WebSocket message into MarketTick."""
        try:
            if isinstance(message, bytes):
                # Binary message - parse according to DhanHQ protocol
                # This is a simplified implementation
                return None

            data = json.loads(message)

            # Skip non-tick messages
            if "type" in data and data["type"] != "tick":
                return None

            return MarketTick(
                symbol=data.get("symbol", ""),
                exchange=Exchange(data.get("exchange", "NSE")),
                ltp=float(data.get("ltp", 0)),
                ltq=int(data.get("ltq", 0)),
                volume=int(data.get("volume", 0)),
                open=float(data.get("open", 0)),
                high=float(data.get("high", 0)),
                low=float(data.get("low", 0)),
                close=float(data.get("close", 0)),
                change=float(data.get("change", 0)),
                change_percent=float(data.get("change_percent", 0)),
                timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
                bid=float(data.get("bid", 0)),
                ask=float(data.get("ask", 0)),
                bid_qty=int(data.get("bid_qty", 0)),
                ask_qty=int(data.get("ask_qty", 0)),
            )
        except Exception as e:
            logger.debug(f"Failed to parse message: {e}")
            return None

    async def _distribute(self, tick: MarketTick) -> None:
        """Distribute tick to all subscribers and callbacks."""
        # Send to queue subscribers
        for queue in self._subscribers:
            try:
                queue.put_nowait(tick)
            except asyncio.QueueFull:
                # Drop oldest item if queue is full
                try:
                    queue.get_nowait()
                    queue.put_nowait(tick)
                except asyncio.QueueEmpty:
                    pass

        # Call registered callbacks
        for callback in self._callbacks:
            try:
                callback(tick)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    async def _resubscribe(self) -> None:
        """Re-subscribe to all previously subscribed symbols."""
        if self._subscribed_symbols:
            symbols = list(self._subscribed_symbols)
            self._subscribed_symbols.clear()
            await self.subscribe(symbols)


# Convenience function for creating a data feed
def create_market_feed() -> MarketDataFeed:
    """Create a new market data feed instance."""
    return MarketDataFeed()
