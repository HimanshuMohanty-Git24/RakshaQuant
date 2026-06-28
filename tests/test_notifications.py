from unittest.mock import AsyncMock, patch

import pytest

from src.notifications.telegram import TelegramNotifier, get_notifier


@pytest.fixture
def mock_settings():
    with patch("src.notifications.telegram.get_settings") as mock:
        mock.return_value.telegram_bot_token = "fake_token"
        mock.return_value.telegram_chat_id = "fake_chat_id"
        yield mock


@pytest.fixture
def notifier(mock_settings):
    # Reset global
    import src.notifications.telegram

    src.notifications.telegram._notifier = None
    return TelegramNotifier()


def test_notifier_init(notifier):
    assert notifier.bot_token == "fake_token"
    assert notifier.chat_id == "fake_chat_id"
    assert notifier.enabled is True


def test_notifier_init_disabled():
    with patch("src.notifications.telegram.get_settings") as mock:
        mock.return_value.telegram_bot_token = None
        mock.return_value.telegram_chat_id = None

        notifier = TelegramNotifier()
        assert notifier.enabled is False


@pytest.mark.asyncio
async def test_send_request(notifier):
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value.__aenter__.return_value = mock_resp

        result = await notifier._send_request("method", {})
        assert result["ok"] is True


@pytest.mark.asyncio
async def test_send_request_fail(notifier):
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.json.return_value = {"ok": False, "description": "error"}
        mock_post.return_value.__aenter__.return_value = mock_resp

        result = await notifier._send_request("method", {})
        assert result is None


@pytest.mark.asyncio
async def test_send_message(notifier):
    with patch.object(notifier, "_send_request", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"ok": True}

        success = await notifier.send_message("hello")
        assert success is True
        mock_send.assert_called_with(
            "sendMessage",
            {
                "chat_id": "fake_chat_id",
                "text": "hello",
                "parse_mode": "Markdown",
                "disable_notification": False,
            },
        )


@pytest.mark.asyncio
async def test_send_trade_alert(notifier):
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        await notifier.send_trade_alert("AAPL", "BUY", 10, 100)
        mock_send.assert_called()
        args = mock_send.call_args[0][0]
        assert "TRADE EXECUTED" in args
        assert "AAPL" in args


@pytest.mark.asyncio
async def test_send_discovery_alert(notifier):
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        stocks = [{"symbol": "AAPL", "score": 10}]
        await notifier.send_discovery_alert(stocks)
        mock_send.assert_called()
        args = mock_send.call_args[0][0]
        assert "STOCK DISCOVERY REPORT" in args
        assert "AAPL" in args


@pytest.mark.asyncio
async def test_send_pnl_summary(notifier):
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        await notifier.send_pnl_summary(1000, 10, 5, 2, 50)
        mock_send.assert_called()
        args = mock_send.call_args[0][0]
        assert "DAILY P&L SUMMARY" in args


@pytest.mark.asyncio
async def test_send_sentiment_alert(notifier):
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        await notifier.send_sentiment_alert(80, "greed", 0.5)
        mock_send.assert_called()
        args = mock_send.call_args[0][0]
        assert "MARKET SENTIMENT UPDATE" in args
        assert "greed" in args.lower()


@pytest.mark.asyncio
async def test_send_error_alert(notifier):
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        await notifier.send_error_alert("Something went wrong")
        mock_send.assert_called()
        args = mock_send.call_args[0][0]
        assert "ERROR ALERT" in args


@pytest.mark.asyncio
async def test_send_startup_message(notifier):
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_startup_message()
        mock_send.assert_called()


@pytest.mark.asyncio
async def test_send_shutdown_message(notifier):
    with patch.object(notifier, "send_message", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.send_shutdown_message()
        mock_send.assert_called()


def test_get_notifier(mock_settings):
    # Reset global
    import src.notifications.telegram

    src.notifications.telegram._notifier = None

    n1 = get_notifier()
    n2 = get_notifier()
    assert n1 is n2
    assert isinstance(n1, TelegramNotifier)
