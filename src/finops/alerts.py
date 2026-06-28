"""
FinOps — alerting.

Dispatches operational alerts (budget breaches, drawdown, data staleness, anomalous
loss, spend spikes) to the logs and, when configured, to Telegram. De-duplicates by
key per IST day so a sustained condition does not spam the channel.

Kept separate from cost_tracker so accounting stays I/O-free; this is the async
side that the (async) trading loop awaits.
"""

from __future__ import annotations

import logging

from src.utils.market_time import now_ist

logger = logging.getLogger(__name__)

_LEVEL_TO_LOGGING = {
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class AlertManager:
    """Logs + optional Telegram alerts, de-duplicated per IST day by key."""

    def __init__(self) -> None:
        self._sent: set[str] = set()

    @staticmethod
    def _dedup_key(key: str) -> str:
        return f"{now_ist().date().isoformat()}:{key}"

    def reset(self) -> None:
        self._sent.clear()

    async def alert(
        self,
        key: str,
        message: str,
        level: str = "WARNING",
        once_per_day: bool = True,
    ) -> bool:
        """
        Emit an alert. Returns True if dispatched, False if suppressed as a duplicate.

        Always logs (so there is a record even with Telegram unconfigured); the
        Telegram send is best-effort and never raises.
        """
        dedup = self._dedup_key(key)
        if once_per_day and dedup in self._sent:
            return False
        self._sent.add(dedup)

        logger.log(_LEVEL_TO_LOGGING.get(level, logging.WARNING), "ALERT[%s] %s", key, message)

        try:
            from src.notifications.telegram import get_notifier

            notifier = get_notifier()
            if getattr(notifier, "enabled", False):
                emoji = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🚨", "CRITICAL": "🛑"}.get(
                    level, "⚠️"
                )
                await notifier.send_message(f"{emoji} *{level}*\n{message}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Alert Telegram send failed (non-fatal): %s", exc)

        return True


_alert_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    """Get or create the shared AlertManager."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


def reset_alert_manager() -> None:
    """Reset the shared AlertManager (test isolation)."""
    global _alert_manager
    _alert_manager = None
