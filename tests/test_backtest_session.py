"""Plan M11.1: the backtest is the paper engine over daily bars - next-open fills, stops with
gap fills, the same policy/risk/OMS/costs - and its fills are paper's on the same tape."""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from src.backtesting.bars import bar_path, day_quotes, previous_closes, sessions
from src.backtesting.session import Backtest
from src.domain.clock import ReplayClock
from src.domain.events import FillReceived, OrderSubmitted
from src.domain.types import Bar, OrderType, Side
from src.engine.replay import canonical_events
from src.engine.runner import EngineConfig, build_engine
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.store.event_store import EventStore
from src.utils.market_time import IST
from tests.backtest_fixture import END, START, fixture_bars, universe

GOLDEN = Path(__file__).parent / "golden" / "backtest_fixture.jsonl"
SBIN = "NSE:EQ:SBIN"


def fills(store: EventStore, day: date | None = None) -> list[tuple[Any, ...]]:
    return [(f.fill.instrument_key, f.fill.side.value, f.fill.quantity, f.fill.price, f.fill.charges,
             f.fill.ts) for e in store.read(types=["FillReceived"], ist_date=day)
            if isinstance(f := e.payload, FillReceived)]  # fmt: skip


def test_a_bar_becomes_open_then_low_then_high_then_close():
    bar = next(b for b in fixture_bars() if b.instrument_key == SBIN and not b.adjusted
               and b.session_date == START)  # fmt: skip
    path = bar_path(bar, prev_close=799.0, legs=4)
    times = [q.receipt_ts.astimezone(IST).time() for q in path]
    prices = [q.ltp for q in path]
    assert times[0] == time(9, 15) and times[1] == time(9, 21) and times[-1] == time(15, 25)
    assert times == sorted(times) and prices[:2] == [bar.open, bar.open]
    assert prices[2:6][-1] == bar.low and prices[6:10][-1] == bar.high and prices[-1] == bar.close
    assert prices.index(min(prices)) < prices.index(max(prices))  # adverse first, for longs
    assert all(q.prev_close == 799.0 for q in path)
    volumes = [q.volume_cum for q in path]
    assert volumes == sorted(volumes) and volumes[-1] == bar.volume
    assert all(not b.adjusted for b in sessions(fixture_bars())[START])
    assert previous_closes(fixture_bars(), START)[SBIN] != bar.close


async def test_the_backtest_trades_like_paper_and_is_deterministic(tmp_path):
    bt = Backtest(fixture_bars(), universe())
    result = await bt.run(tmp_path / "one.db", START, END)
    assert result.sessions and not result.skipped
    reasons = {(t.instrument_key, t.exit_reason) for t in result.trades}
    assert (SBIN, "stop") in reasons and ("NSE:EQ:TCS", "target") in reasons
    assert set(result.equity) == {"A"} and len(result.equity["A"]) == len(result.sessions)
    with EventStore(tmp_path / "one.db") as store:
        entries = [e.payload.order for e in store.read(types=["OrderSubmitted"])
                   if isinstance(e.payload, OrderSubmitted) and e.payload.order.intent.side is Side.BUY]  # fmt: skip
        first = fills(store)
        canonical = canonical_events(store)
    for order in entries:  # next-open fills: the 09:21 quote, the day's open
        submitted = order.submitted_at.astimezone(IST).time() if order.submitted_at else None
        assert submitted is not None and time(9, 20) <= submitted < time(9, 21)
    entry_fills = [f for f in first if f[1] == "BUY"]
    assert entry_fills and all(f[5].astimezone(IST).time() == time(9, 21) for f in entry_fills)

    again = await Backtest(fixture_bars(), universe()).run(tmp_path / "two.db", START, END)
    with EventStore(tmp_path / "two.db") as store:
        assert canonical_events(store) == canonical and fills(store) == first
    assert [t.trade_id for t in again.trades] == [t.trade_id for t in result.trades]
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text("\n".join(canonical) + "\n", encoding="utf-8", newline="\n")
    assert canonical == GOLDEN.read_text(encoding="utf-8").splitlines(), (
        "the backtest changed: inspect, then regenerate with UPDATE_GOLDEN=1"
    )


async def test_a_backtest_session_fills_exactly_like_a_paper_session_on_the_same_tape(tmp_path):
    """Run to the day before, copy the store; then the backtest's session and a plain paper
    engine (another environment, its own wiring) on the same quotes produce the same fills."""
    bars = fixture_bars()
    bt = Backtest(bars, universe())
    result = await bt.run(tmp_path / "bt.db", START, END)
    stop_day = next(t.exit_ts.astimezone(IST).date() for t in result.trades
                    if t.instrument_key == SBIN)  # fmt: skip
    days = bt.calendar.trading_days(START, stop_day)
    await bt.run(tmp_path / "before.db", START, days[-2])
    for suffix in ("", "-wal", "-shm"):
        src = tmp_path / f"before.db{suffix}"
        if src.exists():
            shutil.copy(src, tmp_path / f"paper.db{suffix}")
    with EventStore(tmp_path / "before.db") as store:
        assert await bt.run_session(store, stop_day)
        backtested = fills(store, stop_day)
    with EventStore(tmp_path / "paper.db") as store:
        clock = ReplayClock(datetime.combine(stop_day, time(8, 50), IST))
        quotes = day_quotes(sessions(bars)[stop_day], previous_closes(bars, stop_day))
        engine = build_engine(
            config=EngineConfig(environment="paper"), clock=clock, calendar=bt.calendar,
            store=store, quotes=TapeQuoteSource(quotes, clock=clock),
            history=TapeHistorySource([b for b in bars if b.session_date < stop_day]),
            universe=universe(), limits=bt.limits,
        )  # fmt: skip
        run = asyncio.create_task(engine.run())
        while not run.done():
            await clock.advance(300)
        papered = fills(store, stop_day)
    assert backtested == papered and any(f[0] == SBIN and f[1] == "SELL" for f in backtested)


async def test_a_gap_through_a_stop_fills_at_the_open(tmp_path):
    bars = fixture_bars()
    result = await Backtest(bars, universe()).run(tmp_path / "base.db", START, END)
    stop = next(t for t in result.trades if t.instrument_key == SBIN and t.exit_reason == "stop")
    day = stop.exit_ts.astimezone(IST).date()
    with EventStore(tmp_path / "base.db") as store:
        triggers = [e.payload.order.intent.trigger_price for e in store.read(types=["OrderSubmitted"])
                    if isinstance(e.payload, OrderSubmitted)
                    and e.payload.order.intent.instrument.key == SBIN
                    and e.payload.order.intent.order_type is OrderType.SL_M
                    and e.ist_date == day]  # fmt: skip
    trigger = float(triggers[-1])
    gap_open = round(trigger * 0.97, 2)  # the session opens 3% below the stop

    def gapped(b: Bar) -> Bar:
        if b.instrument_key != SBIN or b.session_date != day:
            return b
        return b.model_copy(update={"open": gap_open, "low": min(b.low, gap_open),
                                    "high": max(b.high, gap_open)})  # fmt: skip

    gap = await Backtest([gapped(b) for b in bars], universe()).run(tmp_path / "gap.db", START, day)
    exit_ = next(t for t in gap.trades if t.instrument_key == SBIN)
    # The day's stop is re-placed as the session opens, after the 09:15 quote: it triggers on
    # the first quote while it is live, the 09:21 one - still the open price.
    assert exit_.exit_reason == "stop" and exit_.exit_ts.astimezone(IST).time() <= time(9, 21)
    assert float(exit_.exit_price) <= gap_open  # the gap, not the stop price
    assert float(exit_.exit_price) > gap_open * 0.99  # only the fill model's spread/impact
    assert float(stop.exit_price) > gap_open  # without the gap it filled near its stop
