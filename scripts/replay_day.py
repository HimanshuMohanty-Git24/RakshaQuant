"""
Replay a recorded day through the engine (plan M8.5): the day's tape, the reference snapshots
cached for it, the announcements the live run stored, and cached LLM and decision-model responses
(no network). Writes a fresh event store and the day's report.

    uv run python scripts/replay_day.py --date 2026-10-05 [--out var/replays/2026-10-05]
                                        [--source var/paper/rakshaquant.db]
"""

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.config import get_settings
from src.engine.replay import replay_day
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--source", type=Path, default=None, help="the live run's event store")
    args = parser.parse_args()
    settings = get_settings()
    out = args.out or settings.var_dir / "replays" / args.date.isoformat()
    db = asyncio.run(replay_day(settings, args.date, out_dir=out,
                                source_db=args.source or settings.db_path))  # fmt: skip
    print(f"replayed {args.date}: {db} (report in {out})")
    return ExitCode.OK


if __name__ == "__main__":
    run_entry_point("replay_day", main, console_log_level="WARNING")
