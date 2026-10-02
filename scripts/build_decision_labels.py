"""
Build the labelled announcement set for decision-model calibration (plan M7.5).

    # 1. collect: announcements our RSS reader stored, plus (optionally) a CSV downloaded by hand
    #    from https://www.nseindia.com/companies-listing/corporate-filings-announcements
    uv run python scripts/build_decision_labels.py collect [--csv announcements.csv]

    # 2. label with the LLM role `label` (paid calls). Without --confirm-spend this is a dry run
    #    that prints how many calls are needed and the estimated cost.
    uv run python scripts/build_decision_labels.py label [--limit 300] [--confirm-spend]

    # 3. export a sample for the owner to check, then merge the corrections back
    uv run python scripts/build_decision_labels.py spotcheck [--n 50]
    uv run python scripts/build_decision_labels.py merge var/datasets/spotcheck_corrected.csv

The dataset is ``<var>/datasets/announcements_labels.parquet``.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import pandas as pd

from src.config import get_settings
from src.config.errors import ConfigError
from src.decision_models import labels
from src.domain.clock import WallClock
from src.domain.events import AnnouncementReceived
from src.llm.pricing import PricingTable
from src.llm.registry import validate_roles
from src.llm.setup import build_router
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point
from src.reference.instruments import build_instruments
from src.reference.universe import NIFTY50_NAME, load_universe
from src.store.event_store import EventStore
from src.store.sink import StoreSink


def dataset_path() -> Path:
    return get_settings().datasets_dir / "announcements_labels.parquet"


def _universe() -> list:  # type: ignore[type-arg]
    settings = get_settings()
    snapshots = sorted(settings.reference_dir.glob(f"{NIFTY50_NAME}_*.csv"))
    if not snapshots:
        raise ConfigError("no universe snapshot in var/reference: run the engine once first")
    return list(build_instruments(load_universe(snapshots[-1]), None).by_symbol.values())


def collect(csv: Path | None) -> int:
    settings = get_settings()
    frames = [labels.load(dataset_path())]
    if settings.db_path.exists():
        with EventStore(settings.db_path) as store:
            stored = [e.payload for e in store.read(types=["AnnouncementReceived"])
                      if isinstance(e.payload, AnnouncementReceived)]  # fmt: skip
        frames.append(labels.from_announcements(stored))
    if csv is not None:
        frames.append(labels.from_nse_csv(csv, _universe()))
    merged = labels.merge(*frames)
    labels.save(merged, dataset_path())
    unlabelled = int(merged["label_event_type"].isna().sum())
    print(f"{len(merged)} announcements ({unlabelled} unlabelled) -> {dataset_path()}")
    return ExitCode.OK


async def label(limit: int | None, confirm: bool) -> int:
    settings = get_settings()
    roles = validate_roles(settings)
    role = roles.get("label")
    if role is None or not role.enabled:
        raise ConfigError(
            "set LLM_ROLE_LABEL=provider:model in .env (e.g. anthropic:claude-opus-5-5)"
        )
    df = labels.load(dataset_path())
    todo = df[df["label_event_type"].isna()]
    if limit is not None:
        todo = todo.head(limit)
    calls, inr = labels.estimate_cost(todo, role.chain[0], PricingTable.from_yaml(),
                                      settings.usd_inr)  # fmt: skip
    cost = "unknown (no price for the model)" if inr is None else f"~Rs {inr:.2f}"
    print(f"{calls} teacher calls on {role.chain[0]} - estimated cost {cost}")
    if not confirm:
        print("dry run: re-run with --confirm-spend to make the calls")
        return ExitCode.OK
    clock = WallClock()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with EventStore(settings.db_path) as store:
        router = build_router(settings, clock=clock, sink=StoreSink(store, clock, "labels"),
                              store=store)  # fmt: skip
        labelled = await labels.teacher_label(df, router, limit=limit)
    labels.save(labelled, dataset_path())
    done = int(labelled["label_event_type"].notna().sum())
    print(f"{done}/{len(labelled)} labelled; spent today Rs {router.ledger.total}")
    return ExitCode.OK


def spotcheck(n: int) -> int:
    out = get_settings().datasets_dir / "spotcheck.csv"
    sample = labels.spotcheck(labels.load(dataset_path()), n)
    sample.to_csv(out, index=False, encoding="utf-8")
    print(f"{len(sample)} rows -> {out}: fill the correct_* columns where a label is wrong")
    return ExitCode.OK


def merge(corrections: Path) -> int:
    df, changed = labels.merge_corrections(labels.load(dataset_path()),
                                           pd.read_csv(corrections, dtype=str))  # fmt: skip
    labels.save(df, dataset_path())
    print(f"{changed} rows corrected")
    return ExitCode.OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--csv", type=Path)
    lab = sub.add_parser("label")
    lab.add_argument("--limit", type=int)
    lab.add_argument("--confirm-spend", action="store_true")
    s = sub.add_parser("spotcheck")
    s.add_argument("--n", type=int, default=50)
    m = sub.add_parser("merge")
    m.add_argument("corrections", type=Path)
    args = parser.parse_args()
    if args.command == "collect":
        return collect(args.csv)
    if args.command == "label":
        return asyncio.run(label(args.limit, args.confirm_spend))
    if args.command == "spotcheck":
        return spotcheck(args.n)
    return merge(args.corrections)


if __name__ == "__main__":
    run_entry_point("build_decision_labels", main, console_log_level="WARNING")
