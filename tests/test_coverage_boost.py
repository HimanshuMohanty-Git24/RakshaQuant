import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.market.data_feed import Exchange, MarketDataFeed, MarketTick
from src.market.websocket_feed import (
    DhanWebSocketFeed,
    ExchangeSegment,
    QuoteData,
    TickerData,
)

# --- MarketDataFeed Tests ---


@pytest.fixture
def market_data_feed():
    with patch("src.market.data_feed.get_settings") as mock_settings:
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"
        return MarketDataFeed()


@pytest.mark.asyncio
async def test_market_feed_connect(market_data_feed):
    # Mock settings again inside test to ensure it's picked up by connect()
    with patch("src.market.data_feed.get_settings") as mock_settings:
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"
        with patch(
            "src.market.data_feed.websockets.connect", new_callable=AsyncMock
        ) as mock_connect:
            await market_data_feed.connect()
            assert market_data_feed._running is True
            mock_connect.assert_called()


@pytest.mark.asyncio
async def test_market_feed_disconnect(market_data_feed):
    mock_ws = AsyncMock()
    market_data_feed._ws = mock_ws
    market_data_feed._running = True
    await market_data_feed.disconnect()
    assert market_data_feed._running is False
    # Check if close was called on the mock object we captured
    mock_ws.close.assert_called()


@pytest.mark.asyncio
async def test_market_feed_connect_retry(market_data_feed):
    # Mock settings again for retry test
    with patch("src.market.data_feed.get_settings") as mock_settings:
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"
        # Mock websockets.connect to fail once then succeed
        with patch(
            "src.market.data_feed.websockets.connect", new_callable=AsyncMock
        ) as mock_connect:
            mock_connect.side_effect = [Exception("Fail"), AsyncMock()]

            # Speed up retry
            market_data_feed.reconnect_delay = 0.01

            await market_data_feed.connect()
            assert mock_connect.call_count == 2
            assert market_data_feed._running is True


@pytest.mark.asyncio
async def test_dhan_feed_parsing_errors(dhan_feed):
    # Short data
    dhan_feed._parse_ticker(b"short")

    # Malformed data (not enough bytes for unpack)
    bad_data = b"\x02" + b"\x00" * 10
    assert dhan_feed._parse_ticker(bad_data) is None

    # Header error
    with patch.object(dhan_feed, "_parse_header", side_effect=Exception("Header fail")):
        assert dhan_feed._parse_ticker(b"1" * 20) is None


@pytest.mark.asyncio
async def test_market_feed_subscribe(market_data_feed):
    market_data_feed._ws = AsyncMock()
    await market_data_feed.subscribe(["AAPL"], Exchange.NSE)
    market_data_feed._ws.send.assert_called()
    assert "AAPL" in market_data_feed._subscribed_symbols


@pytest.mark.asyncio
async def test_market_feed_unsubscribe(market_data_feed):
    market_data_feed._ws = AsyncMock()
    market_data_feed._subscribed_symbols.add("AAPL")
    await market_data_feed.unsubscribe(["AAPL"])
    market_data_feed._ws.send.assert_called()
    assert "AAPL" not in market_data_feed._subscribed_symbols


def test_market_feed_subscribers(market_data_feed):
    q = asyncio.Queue()
    market_data_feed.add_subscriber(q)
    assert q in market_data_feed._subscribers
    market_data_feed.remove_subscriber(q)
    assert q not in market_data_feed._subscribers


def test_market_feed_parse_message(market_data_feed):
    msg = json.dumps(
        {
            "type": "tick",
            "symbol": "AAPL",
            "exchange": "NSE",
            "ltp": 100,
            "ltq": 10,
            "volume": 1000,
            "timestamp": "2024-01-01T10:00:00",
        }
    )
    tick = market_data_feed._parse_message(msg)
    assert isinstance(tick, MarketTick)
    assert tick.symbol == "AAPL"

    # Binary should return None (not implemented yet)
    assert market_data_feed._parse_message(b"binary") is None

    # Non-tick message
    assert market_data_feed._parse_message('{"type": "other"}') is None


@pytest.mark.asyncio
async def test_market_feed_distribute(market_data_feed):
    q = asyncio.Queue()
    market_data_feed.add_subscriber(q)

    tick = MarketTick("AAPL", Exchange.NSE, 100, 10, 1000, 100, 100, 100, 100, 0, 0, None)
    await market_data_feed._distribute(tick)

    assert q.qsize() == 1
    item = await q.get()
    assert item == tick


@pytest.mark.asyncio
async def test_market_feed_streaming(market_data_feed):
    market_data_feed._ws = AsyncMock()
    # Return one message then raise exception to break loop (simulating disconnect then handled)
    # Actually, better to mock _ws.recv to return a message, then set running=False

    async def mock_recv():
        market_data_feed._running = False  # Stop after one
        return json.dumps({"type": "tick", "symbol": "AAPL"})

    market_data_feed._ws.recv = mock_recv
    market_data_feed._running = True

    # Mock connect to avoid real connection attempt if it tries to reconnect
    with patch.object(market_data_feed, "connect", new_callable=AsyncMock):
        await market_data_feed.start_streaming()


# --- DhanWebSocketFeed Tests (Detailed parsing) ---


@pytest.fixture
def dhan_feed():
    with patch("src.market.websocket_feed.get_settings") as mock:
        mock.return_value.dhan_access_token.get_secret_value.return_value = "t"
        mock.return_value.dhan_client_id = "c"
        return DhanWebSocketFeed()


def test_parse_ticker_packet(dhan_feed):
    # Construct binary packet for Response Code 2 (Ticker)
    # Header: 1 byte code, 2 bytes len, 1 byte exchange, 4 bytes sec_id
    # Body: 4 bytes LTP, 4 bytes LTT

    # Code 2, Len 16, Exch 1 (NSE_EQ), SecID 1234
    header = (
        struct.pack("<B", 2)
        + struct.pack("<H", 16)
        + struct.pack("<B", 1)
        + struct.pack("<I", 1234)
    )
    body = struct.pack("<f", 150.0) + struct.pack("<I", 1600000000)
    data = header + body

    dhan_feed.subscribed_instruments["1234"] = "AAPL"

    ticker = dhan_feed._parse_ticker(data)
    assert isinstance(ticker, TickerData)
    assert ticker.symbol == "AAPL"
    assert ticker.last_price == 150.0
    assert ticker.exchange_segment == "NSE_EQ"


def test_parse_quote_packet(dhan_feed):
    # Response Code 4 (Quote)
    # Header: 8 bytes
    # Body: LTP(4), LQ(2), LTT(4), AvgP(4), Vol(4), TSell(4), TBuy(4), O(4), C(4), H(4), L(4)
    # Total body: 4+2+4+4+4+4+4+4+4+4+4 = 42 bytes

    header = (
        struct.pack("<B", 4)
        + struct.pack("<H", 50)
        + struct.pack("<B", 1)
        + struct.pack("<I", 1234)
    )
    body = (
        struct.pack("<f", 100.0)  # LTP
        + struct.pack("<H", 10)  # LQ
        + struct.pack("<I", 1600000000)  # LTT
        + struct.pack("<f", 99.0)  # AvgP
        + struct.pack("<I", 1000)  # Vol
        + struct.pack("<I", 500)  # TSell
        + struct.pack("<I", 600)  # TBuy
        + struct.pack("<f", 98.0)  # Open
        + struct.pack("<f", 95.0)  # Close
        + struct.pack("<f", 105.0)  # High
        + struct.pack("<f", 97.0)  # Low
    )
    data = header + body

    dhan_feed.subscribed_instruments["1234"] = "AAPL"
    dhan_feed.prev_close_data["1234"] = 90.0

    quote = dhan_feed._parse_quote(data)
    assert isinstance(quote, QuoteData)
    assert quote.symbol == "AAPL"
    assert quote.last_price == 100.0
    assert quote.prev_close == 90.0
    assert quote.change == 10.0


def test_parse_prev_close_packet(dhan_feed):
    # Code 6
    header = (
        struct.pack("<B", 6)
        + struct.pack("<H", 12)
        + struct.pack("<B", 1)
        + struct.pack("<I", 1234)
    )
    body = struct.pack("<f", 100.0)
    data = header + body

    dhan_feed._parse_prev_close(data)
    assert dhan_feed.prev_close_data["1234"] == 100.0


def test_process_binary_message(dhan_feed):
    # Ticker packet
    header = (
        struct.pack("<B", 2)
        + struct.pack("<H", 16)
        + struct.pack("<B", 1)
        + struct.pack("<I", 1234)
    )
    body = struct.pack("<f", 150.0) + struct.pack("<I", 1600000000)
    data = header + body

    callback = MagicMock()
    dhan_feed.on_ticker = callback
    dhan_feed.subscribed_instruments["1234"] = "AAPL"

    dhan_feed._process_binary_message(data)
    callback.assert_called()


def test_process_disconnect_packet(dhan_feed):
    # Code 50
    header = (
        struct.pack("<B", 50) + struct.pack("<H", 10) + struct.pack("<B", 0) + struct.pack("<I", 0)
    )
    body = struct.pack("<H", 1000)  # Code
    data = header + body

    dhan_feed.connected = True
    dhan_feed._process_binary_message(data)
    assert dhan_feed.connected is False


@pytest.mark.asyncio
async def test_dhan_subscribe_batch(dhan_feed):
    dhan_feed.ws = AsyncMock()
    dhan_feed.connected = True

    # Create > 100 instruments to test batching
    instruments = [(ExchangeSegment.NSE_EQ, str(i)) for i in range(150)]

    await dhan_feed.subscribe(instruments)

    # Should be called twice (100 + 50)
    assert dhan_feed.ws.send.call_count == 2
    assert len(dhan_feed.subscribed_instruments) == 150
