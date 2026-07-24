"""
Out-of-sample / walk-forward strategy validation — RUN THIS BEFORE RISKING CAPITAL.

Evaluates the live signal logic (RealSignalStrategy -> the same SignalEngine used live) on
rolling out-of-sample windows, **net of realistic NSE costs**, and prints a blunt
VALIDATED / NOT VALIDATED verdict. A green single in-sample backtest is meaningless; this is
the gate.

    uv run python scripts/validate_strategy.py

NOTE: uses a FIXED, explicit universe (not the live StockDiscovery output, which would inject
look-ahead/selection bias). See the survivorship caveat printed at the end.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtesting.strategies import RealSignalStrategy
from src.backtesting.walk_forward import aggregate_reports, run_walk_forward
from src.execution.costs import CostModel
from src.market.yfinance_feed import YFinanceFeed

# A fixed large-cap universe. Deliberately NOT StockDiscovery (which picks today's movers and
# would bias the evaluation). Replace with a point-in-time, survivorship-free list for production.
DEFAULT_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "SBIN", "ITC", "LT", "AXISBANK", "BHARTIARTL",
]
LINE = "=" * 84


def main(symbols: list[str] | None = None, period: str = "2y", warmup: int = 120, test: int = 40) -> None:
    symbols = symbols or DEFAULT_UNIVERSE
    cost_model = CostModel.from_settings()
    feed = YFinanceFeed(symbols=symbols)
    reports = []

    print(LINE)
    print(" Out-of-Sample / Walk-Forward Validation  (net of realistic costs)")
    print(LINE)
    print(f" Universe: {len(symbols)} symbols | period={period} | warmup={warmup} test={test} bars")
    print(
        f" Costs: slippage {cost_model.slippage_bps}bps | brokerage {cost_model.brokerage_bps}bps"
        f" (cap Rs.{cost_model.brokerage_max:.0f}) | statutory {cost_model.statutory_bps}bps"
        f" | GST {cost_model.gst_pct}%"
    )
    print(LINE)
    header = f"{'Symbol':<12}{'trades':>8}{'ret%':>9}{'exp':>9}{'win%':>7}{'PF':>7}{'maxDD%':>8}{'consist':>9}"
    print(header)
    print("-" * len(header))

    for sym in symbols:
        df = feed.get_historical(sym, period=period)
        if df is None or df.empty or len(df) < warmup + test:
            print(f"{sym:<12}{'(insufficient data)':>30}")
            continue
        report = run_walk_forward(
            df,
            lambda s=sym: RealSignalStrategy(symbol=s),
            symbol=sym,
            warmup_bars=warmup,
            test_bars=test,
            cost_model=cost_model,
        )
        reports.append(report)
        pf = "inf" if report.oos_profit_factor == float("inf") else f"{report.oos_profit_factor:.2f}"
        print(
            f"{sym:<12}{report.oos_trades:>8}{report.oos_return_pct:>9.2f}"
            f"{report.oos_expectancy:>9.3f}{report.oos_win_rate:>7.1f}{pf:>7}"
            f"{report.oos_max_drawdown_pct:>8.2f}{report.fold_consistency:>8.0%}"
        )

    print(LINE)
    agg = aggregate_reports(reports)
    print(
        f" Universe OOS: {agg['total_oos_trades']} trades | summed return "
        f"{agg['summed_oos_return_pct']}% | weighted expectancy {agg['weighted_oos_expectancy']}"
        f" | avg consistency {agg['avg_fold_consistency']:.0%}"
    )
    print(f" Symbols net-positive OOS: {agg['symbols_positive']}/{agg['symbols_evaluated']}")
    print(LINE)
    print(f" VERDICT: {agg['verdict']}")
    for reason in agg["reasons"]:
        print(f"   - {reason}")
    print(LINE)
    print(" SURVIVORSHIP CAVEAT: this universe is current-listed names only. A production")
    print(" go/no-go REQUIRES a point-in-time, survivorship-free universe (incl. delisted/")
    print(" suspended names) — YFinance cannot provide this. VALIDATED here is necessary,")
    print(" NOT sufficient. Also: real fills face circuit limits, gaps, and liquidity that")
    print(" a historical bar backtest cannot fully capture.")
    print(LINE)


if __name__ == "__main__":
    main()
