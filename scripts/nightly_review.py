"""
Nightly post-trade review (plan M8.7): role ``review`` writes structured lessons for each trade
closed on the day; stored as ``TradeReview`` events, never fed back into any book in month 1.
The engine already runs it after the daily report; use this to (re)run it for a day.

    uv run python scripts/nightly_review.py [--date 2026-10-05]
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
from src.domain.clock import WallClock
from src.evaluation.review import review_day
from src.llm.setup import build_router
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.utils.market_time import IST


async def run(day: date) -> int:
    settings = get_settings()
    if not settings.db_path.exists():
        raise ConfigError(f"no event store at {settings.db_path}")
    clock = WallClock()
    with EventStore(settings.db_path) as store:
        sink = StoreSink(store, clock, "review")
        router = build_router(settings, clock=clock, sink=sink, store=store)
        if not router.roles.get("review") or not router.roles["review"].enabled:
            raise ConfigError("set LLM_ROLE_REVIEW=provider:model in .env")
        reviews = await review_day(store, router, day, clock=clock, sink=sink)
    print(f"{len(reviews)} trades reviewed for {day}")
    return ExitCode.OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(IST).date())
    return asyncio.run(run(parser.parse_args().date))


if __name__ == "__main__":
    run_entry_point("nightly_review", main, console_log_level="WARNING")
