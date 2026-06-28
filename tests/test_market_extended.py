from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.market.live_data import LiveMarketData, LiveQuote
from src.market.manager import MarketDataManager, MarketQuote, is_market_open
from src.market.simulated_data import SimulatedMarketData, SimulatedQuote
from src.market.stock_discovery import StockDiscovery
from src.market.websocket_feed import DhanWebSocketFeed, QuoteData
from src.market.yfinance_feed import YFinanceFeed

# --- MarketDataManager Tests ---


@pytest.fixture
def mock_settings():
    with patch("src.market.manager.get_settings") as mock:
        mock.return_value.market_data_source = "simulated"
        mock.return_value.dhan_client_id = "test_id"
        mock.return_value.dhan_access_token.get_secret_value.return_value = "test_token"
        yield mock


def test_is_market_open():
    # is_market_open() now evaluates in IST via utils.market_time.now_ist(),
    # so the clock seam to patch is now_ist (not manager.datetime).
    with patch("src.utils.market_time.now_ist") as mock_now:
        clock = MagicMock()
        mock_now.return_value = clock

        # Weekday 10:00 AM IST - Open
        clock.weekday.return_value = 0  # Monday
        clock.time.return_value = time(10, 0)
        assert is_market_open() is True

        # Weekend - Closed
        clock.weekday.return_value = 5  # Saturday
        assert is_market_open() is False

        # Weekday 8:00 AM IST - Closed (before open)
        clock.weekday.return_value = 0
        clock.time.return_value = time(8, 0)
        assert is_market_open() is False


@pytest.mark.asyncio
async def test_manager_start_simulated(mock_settings):
    mock_settings.return_value.market_data_source = "simulated"
    manager = MarketDataManager(symbols=["RELIANCE"])

    # Mock simulated data loading
    with patch.object(manager.simulated_data, "get_quotes") as mock_get_quotes:
        mock_get_quotes.return_value = {
            "RELIANCE": SimulatedQuote("RELIANCE", 1, 100, 100, 100, 100, 90, 10, 10, 1000)
        }

        is_live = await manager.start()

        assert is_live is False
        assert manager.data_source == "simulated"
        assert "RELIANCE" in manager.quotes
        assert manager.quotes["RELIANCE"].is_live is False


@pytest.mark.asyncio
async def test_manager_start_yfinance(mock_settings):
    mock_settings.return_value.market_data_source = "yfinance"
    manager = MarketDataManager(symbols=["RELIANCE"])

    with patch("src.market.manager.YFinanceFeed") as MockYF:
        mock_yf = MockYF.return_value
        mock_yf.start = AsyncMock(return_value=True)

        is_live = await manager.start()

        assert is_live is True
        assert manager.data_source == "yfinance"
        # Note: manager.is_live is set to False for yfinance in code as it is delayed, but start returns True for active feed
        assert manager.is_live is False


@pytest.mark.asyncio
async def test_manager_start_dhan(mock_settings):
    mock_settings.return_value.market_data_source = "dhan"

    with patch("src.market.manager.is_market_open", return_value=True):
        manager = MarketDataManager(symbols=["RELIANCE"])

        with patch("src.market.manager.DhanWebSocketFeed") as MockWS:
            mock_ws = MockWS.return_value
            mock_ws.connect = AsyncMock(return_value=True)
            mock_ws.subscribe_nse_stocks = AsyncMock()

            is_live = await manager.start()

            assert is_live is True
            assert manager.data_source == "dhan"
            assert manager.is_live is True


def test_manager_on_websocket_quote(mock_settings):
    manager = MarketDataManager()
    quote = QuoteData(
        "RELIANCE", 1, "NSE_EQ", 100, 1, datetime.now(), 100, 1000, 0, 0, 90, 110, 110, 90
    )

    manager._on_websocket_quote(quote)
    assert "RELIANCE" in manager.quotes
    assert manager.quotes["RELIANCE"].last_price == 100


def test_manager_get_trading_candidates(mock_settings):
    manager = MarketDataManager()
    manager.quotes = {
        "A": MarketQuote("A", 100, 100, 100, 100, 100, 10, 10, 1000, False),  # 10%
        "B": MarketQuote("B", 100, 100, 100, 100, 100, 1, 1, 1000, False),  # 1%
    }

    candidates = manager.get_trading_candidates(min_change=5.0)
    assert len(candidates) == 1
    assert candidates[0].symbol == "A"


# --- LiveMarketData Tests ---


@pytest.fixture
def live_market():
    with patch("src.market.live_data.get_settings") as mock_settings:
        mock_settings.return_value.dhan_base_url = "http://test"
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"
        mock_settings.return_value.dhan_client_id = "id"
        yield LiveMarketData()


def test_live_get_quotes(live_market):
    with patch("src.market.live_data.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {
            "status": "success",
            "data": {
                "NSE_EQ": {
                    "2885": {
                        "last_price": 2500,
                        "ohlc": {"open": 2400, "high": 2550, "low": 2400, "close": 2400},
                    }
                }
            },
        }

        quotes = live_market.get_quotes(["RELIANCE"])
        assert "RELIANCE" in quotes
        assert quotes["RELIANCE"].last_price == 2500
        assert quotes["RELIANCE"].is_bullish is True


def test_live_get_trading_candidates(live_market):
    with patch.object(live_market, "get_quotes") as mock_get_quotes:
        mock_get_quotes.return_value = {
            "A": LiveQuote("A", 1, 110, 100, 110, 100, 100, 10, 10.0),
            "B": LiveQuote("B", 2, 100.1, 100, 100.1, 100, 100, 0.1, 0.1),
        }

        candidates = live_market.get_trading_candidates()
        assert len(candidates) == 1
        assert candidates[0].symbol == "A"


# --- SimulatedMarketData Tests ---


def test_simulated_get_quotes():
    sim = SimulatedMarketData()
    quotes = sim.get_quotes(["RELIANCE"])

    assert "RELIANCE" in quotes
    assert quotes["RELIANCE"].symbol == "RELIANCE"
    assert quotes["RELIANCE"].last_price > 0


def test_simulated_tick():
    sim = SimulatedMarketData()
    sim.get_quotes()  # Init

    initial_price = sim.current_prices["RELIANCE"]
    sim.tick()
    new_price = sim.current_prices["RELIANCE"]

    assert initial_price != new_price


def test_simulated_get_trading_candidates():
    sim = SimulatedMarketData()
    # Force high volatility to ensure changes
    sim.volatility = 0.5
    candidates = sim.get_trading_candidates(min_change=0.0)
    assert len(candidates) > 0


# --- StockDiscovery Tests ---


@pytest.fixture
def discovery():
    with patch("src.market.stock_discovery.get_settings"):
        return StockDiscovery(max_stocks=10)


def test_extract_stock_mentions(discovery):
    text = "Reliance and TCS are doing well today. Also Bajaj Auto."
    mentions = discovery._extract_stock_mentions(text)
    assert "RELIANCE" in mentions
    assert "TCS" in mentions
    assert "BAJAJ-AUTO" in mentions


def test_discover_from_news(discovery):
    with patch("src.market.stock_discovery.feedparser.parse") as mock_parse:
        mock_parse.return_value.entries = [
            {"title": "Reliance surges", "summary": "Reliance hits new high"},
            {"title": "Market down", "summary": "Nothing happening"},
        ]

        mentions = discovery.discover_from_news()
        # Since the code loops over 3 queries, and we mock the response for all of them,
        # "Reliance" will be found 3 times (once per query)
        assert mentions.get("RELIANCE") == 3


def test_discover_market_movers(discovery):
    with patch("src.market.stock_discovery.yf.Tickers") as mock_tickers:
        mock_ticker_obj = MagicMock()
        # history() returns a DataFrame-like object
        mock_hist = MagicMock()
        mock_hist.empty = False
        mock_hist.__len__.return_value = 2
        # iloc should return a dict-like object for -1 and -2
        mock_hist.iloc = MagicMock()

        def iloc_side_effect(arg):
            if arg == -1:
                return {"Close": 110}
            if arg == -2:
                return {"Close": 100}
            return {"Close": 100}

        mock_hist.iloc.__getitem__.side_effect = iloc_side_effect

        mock_ticker_obj.history.return_value = mock_hist

        # The Tickers object has a .tickers attribute which is a dict of symbol -> Ticker
        mock_tickers_instance = mock_tickers.return_value
        # We need to ensure that when we access .tickers.get(), it returns our mock ticker object
        mock_tickers_instance.tickers.get.return_value = mock_ticker_obj

        movers = discovery.discover_market_movers(min_change=5.0)
        # Should find movers because we mocked 10% gain
        assert len(movers) > 0


@pytest.mark.asyncio
async def test_discover(discovery):
    with (
        patch.object(discovery, "discover_from_news", return_value={"RELIANCE": 5}),
        patch.object(discovery, "discover_market_movers", return_value=[]),
    ):
        stocks = await discovery.discover()
        assert "RELIANCE" in stocks
        # Should also have fallback stocks
        assert len(stocks) >= 10


# --- DhanWebSocketFeed Tests ---


@pytest.mark.asyncio
async def test_websocket_feed_connect():
    with patch(
        "src.market.websocket_feed.websockets.connect", new_callable=AsyncMock
    ):
        with patch("src.market.websocket_feed.get_settings") as mock_settings:
            mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"
            mock_settings.return_value.dhan_client_id = "id"

            feed = DhanWebSocketFeed()
            success = await feed.connect()
            assert success is True
            assert feed.connected is True


@pytest.mark.asyncio
async def test_websocket_feed_subscribe():
    with patch("src.market.websocket_feed.websockets.connect", new_callable=AsyncMock):
        with patch("src.market.websocket_feed.get_settings") as mock_settings:
            mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"
            mock_settings.return_value.dhan_client_id = "id"

            feed = DhanWebSocketFeed()
            # Fake connection
            feed.ws = AsyncMock()
            feed.connected = True

            success = await feed.subscribe_nse_stocks(["RELIANCE"])
            assert success is True
            feed.ws.send.assert_called()


# --- YFinanceFeed Tests ---


def test_yfinance_feed_fetch():
    with patch("src.market.yfinance_feed.yf.Tickers") as mock_tickers:
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 150.0
        mock_ticker.history.return_value.iloc = [
            {"Open": 140, "High": 155, "Low": 138, "Close": 145, "Volume": 1000},  # Prev
            {"Open": 145, "High": 152, "Low": 148, "Close": 150, "Volume": 2000},  # Latest
        ]
        # Make history return valid dataframe
        mock_ticker.history.return_value.empty = False
        mock_ticker.history.return_value.__len__.return_value = 2

        mock_tickers.return_value.tickers.get.return_value = mock_ticker

        feed = YFinanceFeed(symbols=["RELIANCE"])
        quotes = feed.fetch_quotes()

        assert "RELIANCE" in quotes
        assert quotes["RELIANCE"].last_price == 150.0


def test_yfinance_get_nifty50():
    with patch("src.market.yfinance_feed.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.history.return_value.iloc = [
            {"Close": 10000},
            {"Close": 10100, "High": 10200, "Low": 10050, "Volume": 5000},
        ]
        mock_ticker.return_value.history.return_value.empty = False
        mock_ticker.return_value.history.return_value.__len__.return_value = 2

        feed = YFinanceFeed()
        nifty = feed.get_nifty50()

        assert nifty["symbol"] == "NIFTY50"
        assert nifty["last_price"] == 10100
