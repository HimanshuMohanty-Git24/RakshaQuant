"""
Telegram Notification Module

Sends trading alerts to Telegram for mobile notifications.

Features:
- Trade execution alerts
- Daily P&L summary
- Discovery reports
- Error notifications
- Control commands

Setup:
1. Create bot via @BotFather on Telegram
2. Get your chat ID via @userinfobot
3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from src.config import get_settings

logger = logging.getLogger(__name__)


# Telegram Bot API base URL
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class TelegramNotifier:
    """
    Telegram notification service.

    Sends formatted messages to a Telegram chat.
    """

    bot_token: str | None = None
    chat_id: str | None = None
    enabled: bool = True

    def __post_init__(self):
        """Load config if not provided."""
        if self.bot_token is None or self.chat_id is None:
            try:
                settings = get_settings()
                self.bot_token = getattr(settings, "telegram_bot_token", None)
                self.chat_id = getattr(settings, "telegram_chat_id", None)
            except Exception:
                pass

        if not self.bot_token or not self.chat_id:
            self.enabled = False
            logger.warning("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    def _get_url(self, method: str) -> str:
        """Get Telegram API URL."""
        return TELEGRAM_API.format(token=self.bot_token, method=method)

    async def _send_request(self, method: str, data: dict) -> dict | None:
        """Send request to Telegram API."""
        if not self.enabled:
            logger.debug("Telegram disabled, skipping notification")
            return None

        try:
            async with aiohttp.ClientSession() as session:
                url = self._get_url(method)
                async with session.post(url, json=data) as response:
                    result = await response.json()

                    if not result.get("ok"):
                        logger.error(f"Telegram error: {result.get('description')}")
                        return None

                    return result
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return None

    async def send_message(
        self,
        text: str,
        parse_mode: str = "Markdown",
        disable_notification: bool = False,
    ) -> bool:
        """
        Send a text message.

        Args:
            text: Message text (supports Markdown)
            parse_mode: "Markdown" or "HTML"
            disable_notification: Send silently

        Returns:
            True if sent successfully
        """
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        result = await self._send_request("sendMessage", data)
        return result is not None

    # ==========================================
    # Trading Alert Templates
    # ==========================================

    async def send_trade_alert(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        strategy: str = "",
        confidence: float = 0.0,
    ) -> bool:
        """Send trade execution alert."""
        emoji = "🟢" if side == "BUY" else "🔴"

        message = f"""
{emoji} *TRADE EXECUTED*

📊 *{symbol}*
• Side: `{side}`
• Quantity: `{quantity}`
• Price: ₹`{price:,.2f}`
• Value: ₹`{price * quantity:,.2f}`

📈 Strategy: `{strategy or "N/A"}`
🎯 Confidence: `{confidence:.1%}`

⏰ {datetime.now().strftime("%H:%M:%S")}
"""
        return await self.send_message(message.strip())

    async def send_discovery_alert(
        self,
        stocks: list[dict[str, Any]],
    ) -> bool:
        """Send stock discovery report."""
        if not stocks:
            return False

        lines = ["🔍 *STOCK DISCOVERY REPORT*\n"]

        for i, stock in enumerate(stocks[:10], 1):
            symbol = stock.get("symbol", "N/A")
            source = stock.get("source", "N/A")
            score = stock.get("score", 0)
            reason = stock.get("reason", "")[:30]

            emoji = "📰" if source == "news" else "📈" if "gainer" in source else "📉"
            lines.append(f"{i}. {emoji} *{symbol}* (Score: {score:.0f})")
            lines.append(f"   _{reason}_\n")

        lines.append(f"\n⏰ {datetime.now().strftime('%H:%M:%S')}")

        return await self.send_message("\n".join(lines))

    async def send_pnl_summary(
        self,
        balance: float,
        realized_pnl: float,
        unrealized_pnl: float,
        total_trades: int,
        win_rate: float,
        positions: list[dict] | None = None,
    ) -> bool:
        """Send daily P&L summary."""
        total_pnl = realized_pnl + unrealized_pnl
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"

        message = f"""
{pnl_emoji} *DAILY P&L SUMMARY*

💰 Balance: ₹`{balance:,.2f}`
✅ Realized P&L: ₹`{realized_pnl:+,.2f}`
⏳ Unrealized P&L: ₹`{unrealized_pnl:+,.2f}`
📊 *Total P&L: ₹`{total_pnl:+,.2f}`*

📈 Total Trades: `{total_trades}`
🎯 Win Rate: `{win_rate:.1f}%`
"""

        if positions:
            message += "\n*Open Positions:*\n"
            for pos in positions[:5]:
                symbol = pos.get("symbol", "N/A")
                pnl = pos.get("unrealized_pnl", 0)
                pnl_pct = pos.get("unrealized_pnl_pct", 0)
                emoji = "🟢" if pnl >= 0 else "🔴"
                message += f"{emoji} {symbol}: ₹{pnl:+,.0f} ({pnl_pct:+.1f}%)\n"

        message += f"\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}"

        return await self.send_message(message.strip())

    async def send_sentiment_alert(
        self,
        mood_index: int,
        mood_label: str,
        news_score: float,
    ) -> bool:
        """Send market sentiment alert."""
        if mood_index <= 30:
            emoji = "😨"
            color = "🔴"
        elif mood_index <= 50:
            emoji = "😐"
            color = "🟡"
        else:
            emoji = "😊"
            color = "🟢"

        message = f"""
{emoji} *MARKET SENTIMENT UPDATE*

{color} Mood Index: `{mood_index}/100`
📊 Label: `{mood_label.upper()}`
📰 News Score: `{news_score:+.2f}`

⏰ {datetime.now().strftime("%H:%M:%S")}
"""
        return await self.send_message(message.strip())

    async def send_error_alert(
        self,
        error: str,
        context: str = "",
    ) -> bool:
        """Send error notification."""
        message = f"""
⚠️ *ERROR ALERT*

```
{error[:500]}
```

📍 Context: `{context or "N/A"}`
⏰ {datetime.now().strftime("%H:%M:%S")}
"""
        return await self.send_message(message.strip())

    async def send_startup_message(self) -> bool:
        """Send startup notification."""
        message = f"""
🚀 *RakshaQuant Started*

The trading system is now active.

⏰ {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
"""
        return await self.send_message(message.strip())

    async def send_shutdown_message(
        self,
        reason: str = "Manual shutdown",
    ) -> bool:
        """Send shutdown notification."""
        message = f"""
🛑 *RakshaQuant Stopped*

Reason: `{reason}`

⏰ {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
"""
        return await self.send_message(message.strip())


# Global notifier instance
_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    """Get or create the global Telegram notifier."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


async def test_telegram():
    """Test Telegram notifications."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[TELEGRAM] RakshaQuant - Telegram Test")
    print("=" * 60)

    notifier = TelegramNotifier()

    if not notifier.enabled:
        print("\n[SKIP] Telegram not configured.")
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        print("\nTo get these:")
        print("1. Create bot: Talk to @BotFather on Telegram")
        print("2. Get chat ID: Talk to @userinfobot on Telegram")
        return

    print("\n[TEST] Sending test trade alert...")
    success = await notifier.send_trade_alert(
        symbol="RELIANCE",
        side="BUY",
        quantity=10,
        price=2500.00,
        strategy="momentum",
        confidence=0.85,
    )
    print(f"  Result: {'✅ Sent' if success else '❌ Failed'}")

    print("\n[TEST] Sending P&L summary...")
    success = await notifier.send_pnl_summary(
        balance=1000500.00,
        realized_pnl=500.00,
        unrealized_pnl=200.00,
        total_trades=5,
        win_rate=80.0,
    )
    print(f"  Result: {'✅ Sent' if success else '❌ Failed'}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Telegram test complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_telegram())
