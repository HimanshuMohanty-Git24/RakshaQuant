"""
Dead-man check (plan M12.4): during market hours, send a Telegram alarm when the paper
session's heartbeat is more than 3 minutes old, or missing today. Task Scheduler runs it at
10:00 and 13:00 IST on weekdays (``scripts/install_windows_task.ps1``).

    uv run python scripts/deadman_check.py

It only reads the event store. Exit 2 means Telegram is not configured, so an alarm could not
be sent.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import get_settings  # noqa: E402
from src.domain.calendar import get_calendar  # noqa: E402
from src.evaluation.daily_report import send_summary  # noqa: E402
from src.notifications.telegram import TelegramNotifier  # noqa: E402
from src.ops.deadman import alarm_text, check  # noqa: E402
from src.ops.exit_codes import ExitCode  # noqa: E402
from src.ops.process import run_entry_point  # noqa: E402

TELEGRAM_TIMEOUT_S = 10.0


def main() -> int:
    settings = get_settings()
    result = check(settings.db_path, now=datetime.now(UTC), calendar=get_calendar())
    print(f"dead-man check ({settings.environment}): {result.status} - {result.detail}")
    if not result.alarm:
        return ExitCode.OK
    notifier = TelegramNotifier()
    if not notifier.enabled:
        print("Telegram is not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID): no alarm sent")
        return ExitCode.CONFIG_ERROR
    sent = asyncio.run(send_summary(alarm_text(result, settings.environment),
                                    notifier.send_message, TELEGRAM_TIMEOUT_S))  # fmt: skip
    print("alarm sent" if sent else "the alarm could not be sent (see the log)")
    return ExitCode.OK if sent else ExitCode.CRASH


if __name__ == "__main__":
    run_entry_point("deadman_check", main, console_log_level="WARNING")
