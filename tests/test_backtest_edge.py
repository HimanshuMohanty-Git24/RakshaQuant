"""Plan M11.2: the edge statistics and the n ≥ 200 / CI > 0 gate."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np

from src.backtesting.bars import calendar_from_bars
from src.backtesting.edge import (
    MIN_TRADES,
    edge_report,
    max_drawdown_pct,
    monte_carlo_drawdown,
    period_return_pct,
    sortino,
    trade_return_pct,
    trade_stats,
)
from src.backtesting.session import Backtest
from src.domain.events import RegimeComputed, TradeClosed
from src.domain.types import Side
from src.store.event_store import EventStore
from tests.backtest_fixture import END, START, fixture_bars, universe

T0 = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def trade(net: str, *, i: int = 0, entry: str = "1000", qty: int = 100, strategy: str = "momentum",
          day: int = 0) -> TradeClosed:  # fmt: skip
    ts = T0 + timedelta(days=day)
    return TradeClosed(trade_id=f"t{i}", book_id="A", decision_id=f"d{i}",
                       instrument_key="NSE:EQ:INFY", strategy=strategy, side=Side.BUY,
                       quantity=qty, entry_price=Decimal(entry), exit_price=Decimal(entry),
                       entry_ts=ts, exit_ts=ts + timedelta(hours=3), gross_pnl=Decimal(net),
                       charges=Decimal(0), net_pnl=Decimal(net), exit_reason="target")  # fmt: skip


def test_per_trade_returns_their_ci_and_t_stat():
    assert trade_return_pct(trade("1500")) == 1.5  # ₹1,500 on a ₹1,00,000 notional
    stats = trade_stats([1.0, 2.0, 3.0, -1.0])
    assert stats.n == 4 and stats.mean_pct == 1.25 and stats.win_rate == 0.75
    sd = np.std([1.0, 2.0, 3.0, -1.0], ddof=1)
    assert stats.t_stat == round(1.25 / (sd / 2), 3)
    assert stats.ci95 is not None and stats.ci95[0] < 1.25 < stats.ci95[1]
    assert trade_stats([1.0, 2.0]).ci95 == trade_stats([1.0, 2.0]).ci95  # seeded
    assert trade_stats([]).n == 0 and trade_stats([]).ci95 is None


def test_the_gate_needs_200_trades_and_a_positive_lower_bound():
    rng = np.random.default_rng(1)
    winners = [trade(f"{x:.2f}", i=i) for i, x in enumerate(rng.normal(400, 900, 250))]
    noise = [trade(f"{x:.2f}", i=i) for i, x in enumerate(rng.normal(0, 900, 250))]
    few = winners[: MIN_TRADES - 1]
    kw = {"equity": {}, "capital": Decimal(1_000_000), "start": START, "end": END, "sessions": 10}
    assert edge_report(winners, **kw).verdict == "VALIDATED"
    report = edge_report(noise, **kw)
    assert report.verdict == "NOT VALIDATED" and "not > 0" in report.reasons[0]
    report = edge_report(few, **kw)
    assert report.verdict == "NOT VALIDATED" and "too few" in report.reasons[0]


def test_drawdowns_sortino_and_benchmarks():
    assert max_drawdown_pct([100, 110, 99, 120, 108]) == 10.0  # from the running peak
    assert sortino([0.01, -0.01, 0.02]) is not None and sortino([0.01, 0.02]) is None
    mc = monte_carlo_drawdown([2.0, -3.0, 1.0, -2.0, 4.0], weight=0.5)
    assert mc is not None and 0 < mc["p50"] <= mc["p95"]
    assert mc == monte_carlo_drawdown([2.0, -3.0, 1.0, -2.0, 4.0], weight=0.5)  # seeded
    closes = {date(2026, 9, 30): 100.0, date(2026, 10, 1): 101.0, date(2026, 10, 16): 110.0}
    assert period_return_pct(closes, date(2026, 10, 1), date(2026, 10, 16)) == 10.0


def test_a_calendar_from_the_data_covers_any_year():
    bars = fixture_bars()
    calendar = calendar_from_bars(bars)
    assert calendar.covers(date(2025, 3, 3)) and calendar.covers(date(2026, 10, 5))
    days = {b.session_date for b in bars}
    assert set(calendar.trading_days(date(2025, 1, 1), date(2025, 12, 31))) == {
        d for d in days if d.year == 2025
    }
    assert not calendar.is_trading_day(date(2026, 10, 2))  # Gandhi Jayanti: no bar that day


async def test_the_report_of_a_backtest(tmp_path):
    bars = fixture_bars()
    result = await Backtest(bars, universe()).run(tmp_path / "bt.db", START, END)
    with EventStore(tmp_path / "bt.db") as store:
        regimes = {e.payload.session_date: e.payload.label.value
                   for e in store.read(types=["RegimeComputed"])
                   if isinstance(e.payload, RegimeComputed)}  # fmt: skip
    closes: dict[str, dict[date, float]] = {}
    for b in bars:
        if not b.adjusted:
            closes.setdefault(b.instrument_key, {})[b.session_date] = b.close
    report = edge_report(result.trades, result.equity["A"], capital=Decimal(1_000_000),
                         start=START, end=END, sessions=len(result.sessions), regimes=regimes,
                         index_closes=closes["NSE:INDEX:NIFTY50"], symbol_closes=closes)  # fmt: skip
    assert report.trades.n == len(result.trades) >= 2
    assert set(report.by_regime) <= set(regimes.values()) | {"unknown"}
    assert report.nifty_return_pct is not None and report.buy_hold_return_pct is not None
    assert report.verdict == "NOT VALIDATED"  # a handful of trades proves nothing
    assert report.as_dict()["trades"]["n"] == report.trades.n


def test_fetched_history_becomes_backtest_bars():
    from src.backtesting.bars import bars_from_history
    from src.marketdata.history import series_from_bars

    bars = [b for b in fixture_bars() if b.session_date < START]
    history = series_from_bars(bars, settled_before=START)
    rebuilt = bars_from_history(history)
    assert len(rebuilt) == len(bars)
    assert {(b.instrument_key, b.session_date, b.adjusted) for b in rebuilt} == {
        (b.instrument_key, b.session_date, b.adjusted) for b in bars
    }


def test_the_validation_script_parses_its_arguments():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "validate_strategy.py"
    spec = importlib.util.spec_from_file_location("validate_strategy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.parse_args(["--start", "2025-01-01", "--end", "2025-12-31",
                              "--strategies", "momentum,breakout", "--universe", "infy,tcs"])  # fmt: skip
    assert args.start == date(2025, 1, 1) and args.strategies == "momentum,breakout"
    import pytest

    with pytest.raises(SystemExit):
        module.parse_args(["--start", "2025-12-31", "--end", "2025-01-01"])
