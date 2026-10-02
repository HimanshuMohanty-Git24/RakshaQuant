"""
Validate strategies before risking capital (plan M11.2): backtest the paper engine itself over
daily history (``src/backtesting/session.py``) and apply the edge gate
(``src/backtesting/edge.py``): VALIDATED needs at least 200 closed trades **and** a 95% bootstrap
CI of the mean net return per trade above zero.

    uv run python scripts/validate_strategy.py --start 2025-01-01 --end 2025-12-31
    uv run python scripts/validate_strategy.py --start 2024-01-01 --end 2025-12-31 \\
        --strategies momentum,breakout --universe INFY,TCS,HDFCBANK --json var/backtests/v.json

History comes from YFinance (``--period`` of it, ending at ``--end``) or, with ``--dataset
bhavcopy``, from the point-in-time NSE dataset (``scripts/fetch_bhavcopy.py``; ``--universe all``
= every EQ name that traded in the period, delisted ones included). SURVIVORSHIP: the YFinance
universe is today's listed names; historical NIFTY constituency is in neither source. A VALIDATED
here is necessary, not sufficient - and the fills are modelled.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.backtesting.bars import bars_from_history  # noqa: E402
from src.backtesting.edge import EdgeReport, edge_report  # noqa: E402
from src.backtesting.session import ENVIRONMENT, Backtest  # noqa: E402
from src.config.limits import load_risk_limits  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from src.decision.engine import DecisionConfig  # noqa: E402
from src.domain.events import RegimeComputed  # noqa: E402
from src.domain.types import Bar, Instrument  # noqa: E402
from src.engine.market import INDEX_KEY, INDEX_TICKER  # noqa: E402
from src.engine.runner import EngineConfig  # noqa: E402
from src.marketdata.bhavcopy import (  # noqa: E402
    BhavcopyStore,
    dataset_bars,
    load_corporate_actions,
)
from src.marketdata.history import YFinanceHistorySource  # noqa: E402
from src.ops.exit_codes import ExitCode  # noqa: E402
from src.ops.process import run_entry_point  # noqa: E402
from src.store.event_store import EventStore  # noqa: E402

# A fixed large-cap universe: deliberately not a list of today's movers (selection bias).
DEFAULT_UNIVERSE = ("RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "ITC", "LT",
                    "AXISBANK", "BHARTIARTL")  # fmt: skip
LINE = "=" * 84


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--universe", default=",".join(DEFAULT_UNIVERSE),
                        help="comma-separated NSE symbols (default: a fixed large-cap list)")  # fmt: skip
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--strategies", default="momentum,mean_reversion",
                        help="comma-separated strategies to trade (the rest are not run)")  # fmt: skip
    parser.add_argument("--period", default="5y", help="YFinance history to fetch (warm-up too)")
    parser.add_argument("--dataset", choices=("yfinance", "bhavcopy"), default="yfinance",
                        help="bhavcopy: the point-in-time NSE dataset (scripts/fetch_bhavcopy.py);"
                             " with --universe all, every EQ name that traded, delisted included")  # fmt: skip
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    parser.add_argument("--strict-halts", action="store_true",
                        help="keep strategy kill switches latched (default: re-arm them each "
                             "morning, as an operator reviewing them would)")  # fmt: skip
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--end is before --start")
    return args


def print_report(report: EdgeReport, strategies: Sequence[str], universe: Sequence[str]) -> None:
    t = report.trades
    print(LINE)
    print(f" Edge validation  {report.start} .. {report.end}  ({report.sessions} sessions)")
    print(f" Strategies: {', '.join(strategies)}  |  universe: {len(universe)} symbols")
    print(LINE)
    ci = "n/a" if t.ci95 is None else f"[{t.ci95[0]:+.3f}%, {t.ci95[1]:+.3f}%]"
    mean = "n/a" if t.mean_pct is None else f"{t.mean_pct:+.3f}%"
    print(f" Trades {t.n}  mean net/trade {mean}  95% CI {ci}  t {t.t_stat}  win {t.win_rate}")
    for name, s in report.by_strategy.items():
        print(f"   {name:<16} n={s.n:<5} mean={s.mean_pct}  CI={s.ci95}")
    for label, s in report.by_regime.items():
        print(f"   regime {label:<9} n={s.n:<5} mean={s.mean_pct}  CI={s.ci95}")
    print(f" Portfolio {report.portfolio_return_pct}%  vs NIFTY {report.nifty_return_pct}%  "
          f"vs buy & hold {report.buy_hold_return_pct}%")  # fmt: skip
    print(f" Sortino {report.sortino}  max drawdown {report.max_drawdown_pct}%  "
          f"Monte Carlo drawdown {report.monte_carlo_drawdown_pct}")  # fmt: skip
    print(LINE)
    print(f" VERDICT: {report.verdict}")
    for reason in report.reasons:
        print(f"   - {reason}")
    print(LINE)
    print(" Necessary, not sufficient: today's listed names (survivorship) unless the universe")
    print(" is point-in-time, and fills are modelled (no circuits, limited liquidity modelling).")
    print(LINE)


async def load(args: argparse.Namespace) -> tuple[list[str], list[Bar]]:
    """The universe and its daily bars (plus NIFTY's, always from YFinance: the regime's input
    and the benchmark; the bhavcopy has no indices)."""
    datasets = get_settings().var_dir / "datasets"
    point_in_time = args.universe.strip().lower() == "all"
    symbols = None if point_in_time else [s.strip().upper() for s in args.universe.split(",")
                                          if s.strip()]  # fmt: skip
    after = args.end + timedelta(days=1)
    if args.dataset == "bhavcopy":
        store = BhavcopyStore(datasets / "bhavcopy")
        actions = load_corporate_actions(datasets / "corporate_actions.parquet")
        if not actions:
            print("  no corporate actions loaded: adjusted prices equal raw ones")
        chosen, bars = dataset_bars(store, args.start, args.end, symbols=symbols, actions=actions)
        index = await YFinanceHistorySource(period=args.period).fetch(
            [], settled_before=after, expected_last=None, extra={INDEX_KEY: INDEX_TICKER})  # fmt: skip
        return chosen, bars + bars_from_history(index)
    if symbols is None:
        raise SystemExit("--universe all needs --dataset bhavcopy (a point-in-time source)")
    history = await YFinanceHistorySource(period=args.period).fetch(
        [Instrument.nse_equity(s) for s in symbols], settled_before=after, expected_last=None,
        extra={INDEX_KEY: INDEX_TICKER},
    )  # fmt: skip
    for key, why in sorted(history.failed.items()):
        print(f"  no history for {key}: {why}")
    return symbols, bars_from_history(history)


async def run(args: argparse.Namespace) -> int:
    strategies = tuple(s.strip() for s in args.strategies.split(",") if s.strip())
    symbols, bars = await load(args)
    universe = [Instrument.nse_equity(s) for s in symbols]
    limits = load_risk_limits().model_copy(update={"enabled_strategies": strategies})
    config = EngineConfig(environment=ENVIRONMENT, heartbeat=False,
                          decision=DecisionConfig(enabled=strategies, shadow=()))  # fmt: skip
    out = get_settings().var_dir / "backtests" / f"{datetime.now():%Y%m%d-%H%M%S}.db"
    backtest = Backtest(bars, universe, config=config, limits=limits,
                        rearm_strategies=not args.strict_halts)  # fmt: skip
    result = await backtest.run(out, args.start, args.end)
    with EventStore(out) as store:
        regimes = {e.payload.session_date: e.payload.label.value
                   for e in store.read(types=["RegimeComputed"])
                   if isinstance(e.payload, RegimeComputed)}  # fmt: skip
    closes: dict[str, dict[date, float]] = {}
    for b in bars:
        if not b.adjusted:
            closes.setdefault(b.instrument_key, {})[b.session_date] = b.close
    report = edge_report(
        result.trades, result.equity.get(config.book_id, {}), capital=config.starting_cash,
        start=args.start, end=args.end, sessions=len(result.sessions), regimes=regimes,
        index_closes=closes.get(INDEX_KEY), symbol_closes=closes,
        position_weight=float(Decimal(str(limits.max_position_pct))),
    )  # fmt: skip
    print_report(report, strategies, symbols)
    halts = "latched (strict)" if args.strict_halts else "re-armed each morning (research)"
    print(f" Strategy halts: {halts}")
    print(f" Events: {out}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    return ExitCode.OK


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    run_entry_point("validate_strategy", main, console_log_level="WARNING")
