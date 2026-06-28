"""
DhanHQ WebSocket Live Market Feed

Real-time market data via WebSocket connection to DhanHQ.
Based on: https://dhanhq.co/docs/v2/live-market-feed/

Features:
- Real-time tick-by-tick data
- Up to 5000 instruments per connection
- Binary packet parsing for speed
- Ticker, Quote, and Full data modes
"""

import asyncio
import json
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

import websockets

from src.config import get_settings

logger = logging.getLogger(__name__)


class FeedRequestCode(IntEnum):
    """Request codes for subscribing to different data modes."""

    SUBSCRIBE_TICKER = 15  # Ticker data (LTP + LTT)
    SUBSCRIBE_QUOTE = 17  # Quote data (OHLC + Volume)
    SUBSCRIBE_FULL = 21  # Full market depth
    UNSUBSCRIBE = 16  # Unsubscribe
    DISCONNECT = 12  # Disconnect


class FeedResponseCode(IntEnum):
    """Response codes from market feed."""

    TICKER_DATA = 2
    QUOTE_DATA = 4
    OI_DATA = 5
    PREV_CLOSE = 6
    FULL_DATA = 8
    DISCONNECT = 50


class ExchangeSegment:
    """Exchange segment identifiers."""

    NSE_EQ = "NSE_EQ"
    NSE_FNO = "NSE_FNO"
    BSE_EQ = "BSE_EQ"
    BSE_FNO = "BSE_FNO"
    MCX = "MCX"

    # Numeric codes from binary responses
    SEGMENT_MAP = {
        1: "NSE_EQ",
        2: "NSE_FNO",
        3: "BSE_EQ",
        4: "BSE_FNO",
        5: "MCX",
    }


@dataclass
class TickerData:
    """Real-time ticker data."""

    symbol: str
    security_id: int
    exchange_segment: str
    last_price: float
    last_trade_time: datetime
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class QuoteData:
    """Real-time quote data with OHLC."""

    symbol: str
    security_id: int
    exchange_segment: str
    last_price: float
    last_quantity: int
    last_trade_time: datetime
    avg_price: float
    volume: int
    total_sell_qty: int
    total_buy_qty: int
    open: float
    close: float
    high: float
    low: float
    prev_close: float = 0.0
    change: float = 0.0
    change_percent: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.prev_close > 0:
            self.change = self.last_price - self.prev_close
            self.change_percent = (self.change / self.prev_close) * 100

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "last_price": self.last_price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "prev_close": self.prev_close,
            "change": self.change,
            "change_percent": self.change_percent,
            "volume": self.volume,
        }


# NSE Stock Security IDs
NSE_WATCHLIST = {
    "RELIANCE": "2885",
    "TCS": "11536",
    "HDFCBANK": "1333",
    "INFY": "1594",
    "ICICIBANK": "4963",
    "HINDUNILVR": "1394",
    "SBIN": "3045",
    "BHARTIARTL": "10604",
    "ITC": "1660",
    "KOTAKBANK": "1922",
    "LT": "11483",
    "AXISBANK": "5900",
    "ASIANPAINT": "236",
    "MARUTI": "10999",
    "TITAN": "3506",
    "BAJFINANCE": "317",
    "WIPRO": "3787",
    "ULTRACEMCO": "11532",
    "NESTLEIND": "17963",
    "TECHM": "13538",
}

# Reverse lookup
SECURITY_ID_TO_SYMBOL = {v: k for k, v in NSE_WATCHLIST.items()}


class DhanWebSocketFeed:
    """
    DhanHQ WebSocket Live Market Feed Client.

    Connects to DhanHQ WebSocket and receives real-time market data.
    """

    def __init__(
        self,
        on_ticker: Callable[[TickerData], None] | None = None,
        on_quote: Callable[[QuoteData], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ):
        self.settings = get_settings()
        self.on_ticker = on_ticker
        self.on_quote = on_quote
        self.on_error = on_error

        self.ws = None
        self.connected = False
        self.subscribed_instruments: dict[str, str] = {}  # security_id -> symbol
        self.prev_close_data: dict[str, float] = {}  # security_id -> prev_close

        # Build WebSocket URL
        token = self.settings.dhan_access_token.get_secret_value()
        client_id = self.settings.dhan_client_id
        self.ws_url = (
            f"wss://api-feed.dhan.co?version=2&token={token}&clientId={client_id}&authType=2"
        )

    async def connect(self):
        """Establish WebSocket connection."""
        try:
            logger.info("Connecting to DhanHQ WebSocket...")
            self.ws = await websockets.connect(
                self.ws_url,
                ping_interval=10,
                ping_timeout=30,
            )
            self.connected = True
            logger.info("WebSocket connected successfully")
            return True
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            if self.on_error:
                self.on_error(e)
            return False

    async def disconnect(self):
        """Disconnect from WebSocket."""
        if self.ws and self.connected:
            try:
                # Send disconnect request
                disconnect_msg = {"RequestCode": FeedRequestCode.DISCONNECT}
                await self.ws.send(json.dumps(disconnect_msg))
                await self.ws.close()
            except Exception:
                pass  # Connection may already be closed
            finally:
                self.connected = False
                logger.info("WebSocket disconnected")

    async def subscribe(
        self,
        instruments: list[tuple[str, str]],  # List of (exchange_segment, security_id)
        mode: FeedRequestCode = FeedRequestCode.SUBSCRIBE_QUOTE,
    ):
        """
        Subscribe to instruments for market data.

        Args:
            instruments: List of (exchange_segment, security_id) tuples
            mode: Data mode (TICKER, QUOTE, or FULL)
        """
        if not self.ws or not self.connected:
            logger.error("WebSocket not connected")
            return False

        # DhanHQ allows max 100 instruments per message
        for i in range(0, len(instruments), 100):
            batch = instruments[i : i + 100]

            instrument_list = [
                {"ExchangeSegment": seg, "SecurityId": sec_id} for seg, sec_id in batch
            ]

            subscribe_msg = {
                "RequestCode": mode,
                "InstrumentCount": len(batch),
                "InstrumentList": instrument_list,
            }

            await self.ws.send(json.dumps(subscribe_msg))

            # Track subscribed instruments
            for seg, sec_id in batch:
                symbol = SECURITY_ID_TO_SYMBOL.get(sec_id, f"UNKNOWN-{sec_id}")
                self.subscribed_instruments[sec_id] = symbol

            logger.info(f"Subscribed to {len(batch)} instruments")

        return True

    async def subscribe_nse_stocks(
        self,
        symbols: list[str] | None = None,
        mode: FeedRequestCode = FeedRequestCode.SUBSCRIBE_QUOTE,
    ):
        """
        Subscribe to NSE stocks by symbol name.

        Args:
            symbols: List of symbols to subscribe, or None for all watchlist
            mode: Data mode
        """
        if symbols is None:
            symbols = list(NSE_WATCHLIST.keys())

        instruments = []
        for symbol in symbols:
            if symbol in NSE_WATCHLIST:
                instruments.append((ExchangeSegment.NSE_EQ, NSE_WATCHLIST[symbol]))

        return await self.subscribe(instruments, mode)

    def _parse_header(self, data: bytes) -> tuple[int, int, int, int]:
        """
        Parse 8-byte response header.

        Returns:
            (response_code, message_length, exchange_segment, security_id)
        """
        response_code = data[0]
        message_length = struct.unpack("<H", data[1:3])[0]
        exchange_segment = data[3]
        security_id = struct.unpack("<I", data[4:8])[0]

        return response_code, message_length, exchange_segment, security_id

    def _parse_ticker(self, data: bytes) -> TickerData | None:
        """Parse ticker data packet (response code 2)."""
        try:
            _, _, exchange_seg, security_id = self._parse_header(data)

            last_price = struct.unpack("<f", data[8:12])[0]
            ltt_epoch = struct.unpack("<I", data[12:16])[0]

            symbol = self.subscribed_instruments.get(str(security_id), f"ID-{security_id}")
            segment = ExchangeSegment.SEGMENT_MAP.get(exchange_seg, "UNKNOWN")

            return TickerData(
                symbol=symbol,
                security_id=security_id,
                exchange_segment=segment,
                last_price=last_price,
                last_trade_time=datetime.fromtimestamp(ltt_epoch),
            )
        except Exception as e:
            logger.error(f"Error parsing ticker: {e}")
            return None

    def _parse_prev_close(self, data: bytes):
        """Parse previous close packet (response code 6)."""
        try:
            _, _, _, security_id = self._parse_header(data)
            prev_close = struct.unpack("<f", data[8:12])[0]
            self.prev_close_data[str(security_id)] = prev_close
        except Exception as e:
            logger.error(f"Error parsing prev close: {e}")

    def _parse_quote(self, data: bytes) -> QuoteData | None:
        """Parse quote data packet (response code 4)."""
        try:
            _, _, exchange_seg, security_id = self._parse_header(data)

            last_price = struct.unpack("<f", data[8:12])[0]
            last_qty = struct.unpack("<H", data[12:14])[0]
            ltt_epoch = struct.unpack("<I", data[14:18])[0]
            avg_price = struct.unpack("<f", data[18:22])[0]
            volume = struct.unpack("<I", data[22:26])[0]
            total_sell_qty = struct.unpack("<I", data[26:30])[0]
            total_buy_qty = struct.unpack("<I", data[30:34])[0]
            open_price = struct.unpack("<f", data[34:38])[0]
            close_price = struct.unpack("<f", data[38:42])[0]
            high_price = struct.unpack("<f", data[42:46])[0]
            low_price = struct.unpack("<f", data[46:50])[0]

            symbol = self.subscribed_instruments.get(str(security_id), f"ID-{security_id}")
            segment = ExchangeSegment.SEGMENT_MAP.get(exchange_seg, "UNKNOWN")
            prev_close = self.prev_close_data.get(str(security_id), close_price)

            return QuoteData(
                symbol=symbol,
                security_id=security_id,
                exchange_segment=segment,
                last_price=last_price,
                last_quantity=last_qty,
                last_trade_time=datetime.fromtimestamp(ltt_epoch),
                avg_price=avg_price,
                volume=volume,
                total_sell_qty=total_sell_qty,
                total_buy_qty=total_buy_qty,
                open=open_price,
                close=close_price,
                high=high_price,
                low=low_price,
                prev_close=prev_close,
            )
        except Exception as e:
            logger.error(f"Error parsing quote: {e}")
            return None

    async def listen(self):
        """
        Listen for market data packets.

        This is a blocking call that processes incoming data.
        """
        if not self.ws or not self.connected:
            logger.error("WebSocket not connected")
            return

        logger.info("Listening for market data...")

        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    self._process_binary_message(message)
                else:
                    # JSON responses (rare)
                    logger.debug(f"JSON message: {message}")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            self.connected = False
        except Exception as e:
            logger.error(f"Error in listen loop: {e}")
            if self.on_error:
                self.on_error(e)

    def _process_binary_message(self, data: bytes):
        """Process incoming binary market data packet."""
        if len(data) < 8:
            return

        response_code = data[0]

        if response_code == FeedResponseCode.TICKER_DATA:
            ticker = self._parse_ticker(data)
            if ticker and self.on_ticker:
                self.on_ticker(ticker)

        elif response_code == FeedResponseCode.QUOTE_DATA:
            quote = self._parse_quote(data)
            if quote and self.on_quote:
                self.on_quote(quote)

        elif response_code == FeedResponseCode.PREV_CLOSE:
            self._parse_prev_close(data)

        elif response_code == FeedResponseCode.DISCONNECT:
            disconnect_code = struct.unpack("<H", data[8:10])[0]
            logger.warning(f"Server disconnected with code: {disconnect_code}")
            self.connected = False


async def test_websocket_feed():
    """Test the WebSocket market feed."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[LIVE] RakshaQuant - WebSocket Market Feed Test")
    print("=" * 60)

    quotes_received = []

    def on_quote(quote: QuoteData):
        quotes_received.append(quote)
        trend = "UP" if quote.change_percent > 0 else "DOWN"
        print(
            f"  {quote.symbol:<12} Rs.{quote.last_price:>10,.2f}  {quote.change_percent:>+6.2f}% [{trend}]"
        )

    def on_error(e: Exception):
        print(f"[ERROR] {e}")

    feed = DhanWebSocketFeed(on_quote=on_quote, on_error=on_error)

    print("\n[CONNECT] Connecting to DhanHQ WebSocket...")
    connected = await feed.connect()

    if not connected:
        print("[FAILED] Could not connect to WebSocket")
        return

    print("[OK] Connected!")

    print("\n[SUBSCRIBE] Subscribing to top 5 NSE stocks...")
    await feed.subscribe_nse_stocks(["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN"])

    print("\n[LISTEN] Waiting for market data (10 seconds)...")

    # Listen for 10 seconds
    try:
        await asyncio.wait_for(feed.listen(), timeout=10)
    except TimeoutError:
        pass

    print(f"\n[RESULT] Received {len(quotes_received)} quote updates")

    await feed.disconnect()
    print("\n[DONE] Test complete")


if __name__ == "__main__":
    asyncio.run(test_websocket_feed())
