"""
Regenerate the daily report (plan M8.4) for any recorded day from the environment's event store.

    uv run python scripts/daily_report.py                    # today (IST)
    uv run python scripts/daily_report.py --date 2026-10-05 [--start 2026-10-05] [--send]

Writes ``<var>/reports/<date>.{md,json}``. NIFTY closes come from the taped index bars, so a
session's NIFTY return is available once the next morning's history has been recorded.
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.config import get_settings
from src.config.errors import ConfigError
from src.domain.calendar import get_calendar
from src.engine.market import INDEX_KEY
from src.evaluation.daily_report import (
    ReportInputs,
    build_report,
    send_summary,
    telegram_summary,
    write_report,
)
from src.evaluation.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point
from src.store.event_store import EventStore
from src.store.tape import read_bars
from src.utils.market_time import IST


def nifty_closes(tape_dir: Path) -> dict[date, float]:
    closes: dict[date, float] = {}
    if not tape_dir.exists():
        return closes
    for folder in sorted(p for p in tape_dir.iterdir() if p.is_dir()):
        try:
            recorded = date.fromisoformat(folder.name)
        except ValueError:
            continue
        for bar in read_bars(tape_dir, recorded):
            if bar.instrument_key == INDEX_KEY and not bar.adjusted:
                closes[bar.session_date] = bar.close
    return closes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(IST).date())
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--send", action="store_true", help="also send the Telegram summary")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.db_path.exists():
        raise ConfigError(f"no event store at {settings.db_path}")
    experiment = load_experiment(settings.experiment_file or DEFAULT_EXPERIMENT_PATH)
    inputs = ReportInputs(
        day=args.date, start=args.start, capital=experiment.capital_inr,
        books=experiment.book_ids,
        advisors={b: spec.advisor for b, spec in experiment.books.items()},
        experiment=experiment.experiment, nifty_closes=nifty_closes(settings.tape_dir),
        infra_cost_inr_per_day=settings.infra_cost_inr_per_day,
    )  # fmt: skip
    with EventStore(settings.db_path) as store:
        report = build_report(store, inputs, get_calendar())
    json_path, md_path = write_report(report, settings.reports_dir)
    print(f"written {md_path} and {json_path.name}")
    if args.send:
        from src.notifications.telegram import TelegramNotifier

        notifier = TelegramNotifier()
        sent = notifier.enabled and asyncio.run(
            send_summary(telegram_summary(report), notifier.send_message)
        )
        print("summary sent" if sent else "summary not sent (Telegram not configured or failed)")
    return ExitCode.OK


if __name__ == "__main__":
    run_entry_point("daily_report", main, console_log_level="WARNING")
