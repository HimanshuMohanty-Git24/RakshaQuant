"""
Build the point-in-time daily dataset from NSE's UDiFF capital-market bhavcopies (plan M11.3).

    uv run python scripts/fetch_bhavcopy.py --start 2024-07-08 --end 2025-12-31
    uv run python scripts/fetch_bhavcopy.py --corporate-actions CF-CA-equities.csv

One request per session (``--delay`` seconds apart), sessions already on disk are skipped, and
a session NSE has no file for (a holiday) is noted and passed over. Files go to
``var/datasets/bhavcopy/<YYYY>/<YYYYMMDD>.parquet``; a corporate-actions CSV exported from NSE's
website is converted to ``var/datasets/corporate_actions.parquet``.

Before running a bulk download, check that NSE's terms of use allow it (PROGRESS OQ-2).
Limitation: historical NIFTY constituency is not in these files.
"""

import argparse
import asyncio
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import httpx2  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from src.config.settings import get_settings  # noqa: E402
from src.marketdata.bhavcopy import (  # noqa: E402
    FIRST_UDIFF_DAY,
    BhavcopyStore,
    parse_corporate_actions,
    parse_udiff,
    url_for,
)
from src.ops.exit_codes import ExitCode  # noqa: E402
from src.ops.process import run_entry_point  # noqa: E402

HEADERS = {
    "user-agent": "Mozilla/5.0 (compatible; RakshaQuant research; daily bhavcopy archive)",
    "accept": "application/zip,*/*",
}


async def fetch(store: BhavcopyStore, start: date, end: date, delay_s: float) -> tuple[int, int]:
    written = missing = 0
    async with httpx2.AsyncClient(timeout=30.0, follow_redirects=True, headers=HEADERS) as client:
        day = max(start, FIRST_UDIFF_DAY)
        while day <= end:
            if day.weekday() < 5 and not store.has(day):
                response = await client.get(url_for(day))
                if response.status_code == 404:
                    missing += 1  # a holiday: no session, no file
                else:
                    response.raise_for_status()
                    rows = parse_udiff(response.content)
                    store.write(day, rows)
                    written += 1
                    print(f"{day}: {len(rows)} rows")
                await asyncio.sleep(delay_s)
            day += timedelta(days=1)
    return written, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    parser.add_argument("--corporate-actions", type=Path, help="NSE corporate-actions CSV export")
    args = parser.parse_args()
    datasets = get_settings().var_dir / "datasets"
    if args.corporate_actions:
        actions = parse_corporate_actions(args.corporate_actions.read_text(encoding="utf-8-sig"))
        table = pa.Table.from_pylist([{**vars(a), "ratio": str(a.ratio) if a.ratio else None,
                                       "amount": str(a.amount) if a.amount else None}
                                      for a in actions])  # fmt: skip
        datasets.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, datasets / "corporate_actions.parquet")
        kinds = {k: sum(1 for a in actions if a.kind == k) for k in ("split", "bonus", "dividend")}
        print(f"{len(actions)} corporate actions ({kinds}, the rest 'other')")
    if args.start:
        started = time.monotonic()
        written, missing = asyncio.run(fetch(BhavcopyStore(datasets / "bhavcopy"), args.start,
                                             args.end, args.delay))  # fmt: skip
        print(f"{written} sessions written, {missing} weekdays without a file "
              f"({time.monotonic() - started:.0f} s)")  # fmt: skip
    return ExitCode.OK


if __name__ == "__main__":
    run_entry_point("fetch_bhavcopy", main, console_log_level="WARNING")
