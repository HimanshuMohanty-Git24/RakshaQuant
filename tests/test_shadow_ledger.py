"""Plan M8.3: the shadow ledger - a counterfactual for every signal, net of costs, with alpha."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from src.brokers.simulated.costs import NSECostSchedule
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.sink import RecordingSink
from src.domain.types import MarketDataSource, Product, Quote, Side, Signal
from src.evaluation.shadow_ledger import ShadowLedger
from src.oms.exit_manager import ExitPolicy
from src.store.event_store import EventStore
from src.store.kv import KVStateStore
from tests.test_books import run as run_day
from tests.test_books import three_books
from tests.test_engine_replay import TCS

T0 = datetime(2026, 10, 5, 3, 55, tzinfo=UTC)  # Mon 09:25 IST
KEY = "NSE:EQ:INFY"
COSTS = NSECostSchedule.from_yaml()


def signal(sid: str = "momentum:INFY", side: Side = Side.BUY, shadow: bool = False) -> Signal:
    return Signal(signal_id=sid, decision_id=f"d-{sid}", instrument_key=KEY, strategy="momentum",
                  side=side, bar_date=date(2026, 10, 1), agreement_score=0.6, stop_atr_mult=2.0,
                  target_atr_mult=3.0, is_shadow=shadow, generated_at=T0)  # fmt: skip


def quote(ltp: float, at: datetime) -> Quote:
    return Quote(instrument_key=KEY, ltp=ltp, prev_close=1000.0, volume_cum=1, exchange_ts=at,
                 receipt_ts=at, source=MarketDataSource.YFINANCE)  # fmt: skip


def ledger(clock: ReplayClock, sink: RecordingSink, store=None, **policy) -> ShadowLedger:
    return ShadowLedger(policy=ExitPolicy(**({"k_stop_atr": 2.0, "k_target_atr": 3.0} | policy)),
                        costs=COSTS, calendar=get_calendar(), clock=clock, sink=sink,
                        state_store=store)  # fmt: skip


def closed(sink: RecordingSink):
    return [e.payload for e in sink.events if e.type == "ShadowTradeClosed"]


async def test_a_counterfactual_enters_after_the_decision_and_exits_at_its_stop_net_of_costs():
    clock = ReplayClock(T0)
    sink = RecordingSink(clock)
    led = ledger(clock, sink)
    assert led.track([signal(), signal(), signal("mr:INFY", Side.SELL)], {KEY: Decimal(20)}) == 1
    await led.on_quote(quote(990.0, T0 - timedelta(minutes=1)))  # before the decision: ignored
    await led.on_quote(quote(1000.0, T0 + timedelta(minutes=1)))  # the entry
    (opened,) = [e.payload for e in sink.events if e.type == "ShadowTradeOpened"]
    assert (opened.entry_price, opened.quantity) == (Decimal("1000.0"), 100)
    assert (opened.stop_price, opened.target_price) == (Decimal(960), Decimal(1060))
    await led.on_quote(quote(975.0, T0 + timedelta(minutes=2)))  # still inside
    await led.on_quote(quote(950.0, T0 + timedelta(minutes=3)))  # gaps through 960: exits at 950
    (c,) = closed(sink)
    buy = COSTS.charges(product=Product.CNC, side=Side.BUY, notional=Decimal(100_000)).total
    sell = COSTS.charges(product=Product.CNC, side=Side.SELL, notional=Decimal(95_000),
                         dp_applies=True).total  # fmt: skip
    assert (c.exit_reason, c.exit_price, c.gross_pnl) == ("stop", Decimal("950.0"), Decimal(-5000))
    assert c.charges == buy + sell and c.net_pnl == Decimal(-5000) - buy - sell
    assert c.net_return_pct < -5 and c.hold_sessions == 0 and led.open_count == 0


async def test_targets_shadow_strategies_and_missing_atr():
    clock = ReplayClock(T0)
    sink = RecordingSink(clock)
    led = ledger(clock, sink)
    assert led.track([signal("breakout:INFY", shadow=True)], {KEY: Decimal(20)}) == 1
    assert led.track([signal("x")], {KEY: None}) == 0
    await led.on_quote(quote(1000.0, T0 + timedelta(minutes=1)))
    await led.on_quote(quote(1061.0, T0 + timedelta(minutes=5)))
    (c,) = closed(sink)
    assert c.exit_reason == "target" and c.is_shadow_strategy and c.net_pnl > 0


async def test_trailing_and_time_exits_follow_the_exit_policy_across_restarts(tmp_path):
    clock = ReplayClock(T0)
    with EventStore(tmp_path / "rq.db") as store:
        sink = RecordingSink(clock)
        kv = KVStateStore(store, "ledger", "shadow")
        led = ledger(clock, sink, kv, k_trail_atr=2.0, max_hold_days=3)
        led.track([signal()], {KEY: Decimal(20)})
        await led.on_quote(quote(1000.0, T0 + timedelta(minutes=1)))

        reborn = ledger(clock, sink, kv, k_trail_atr=2.0, max_hold_days=3)  # a new process
        assert reborn.open_count == 1
        reborn.on_session_start(
            date(2026, 10, 6), closes={KEY: Decimal(1040)}, atr={KEY: Decimal(20)}
        )
        tue = T0 + timedelta(days=1)
        await reborn.on_quote(quote(1005.0, tue))  # trailed stop is 1040 - 40 = 1000: not hit
        assert reborn.open_count == 1
        await reborn.on_quote(quote(999.0, tue + timedelta(minutes=5)))
        (c,) = closed(sink)
        assert c.exit_reason == "stop" and c.exit_price == Decimal("999.0") and c.hold_sessions == 1

        reborn.track([signal("mr:INFY")], {KEY: Decimal(20)})
        await reborn.on_quote(quote(1000.0, tue + timedelta(minutes=6)))
        reborn.on_session_start(date(2026, 10, 9), closes={}, atr={})  # 3 sessions held (Fri)
        fri = T0 + timedelta(days=4)
        await reborn.on_quote(quote(1002.0, fri))
        assert closed(sink)[-1].exit_reason == "time" and closed(sink)[-1].hold_sessions == 3


async def test_alpha_settles_once_the_nifty_closes_are_known():
    clock = ReplayClock(T0)
    sink = RecordingSink(clock)
    led = ledger(clock, sink)
    led.track([signal()], {KEY: Decimal(20)})
    await led.on_quote(quote(1000.0, T0 + timedelta(minutes=1)))
    await led.on_quote(quote(1061.0, T0 + timedelta(minutes=9)))
    assert led.settle_alpha({date(2026, 10, 1): 25_000.0}) == 0  # the exit day's close unknown
    assert led.settle_alpha({date(2026, 10, 1): 25_000.0, date(2026, 10, 5): 25_250.0}) == 1
    (alpha,) = [e.payload for e in sink.events if e.type == "ShadowAlphaSettled"]
    (c,) = closed(sink)
    assert alpha.nifty_return_pct == 1.0 and alpha.entry_date == date(2026, 10, 5)
    assert alpha.alpha_pct == round(c.net_return_pct - 1.0, 4)
    assert led.settle_alpha({date(2026, 10, 1): 1.0, date(2026, 10, 5): 2.0}) == 0  # once


async def test_every_signal_of_the_engine_day_gets_a_counterfactual(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        await run_day(engine, clock)
        store = engine.store
        buys = {e.payload.signal.signal_id for e in store.read(types=["SignalGenerated"])
                if e.payload.signal.side is Side.BUY}  # fmt: skip
        opened = {e.payload.signal_id for e in store.read(types=["ShadowTradeOpened"])}
        assert opened == buys and buys
        outcomes = {e.payload.instrument_key: e.payload for e in store.read(types=["ShadowTradeClosed"])
                    if not e.payload.is_shadow_strategy}  # fmt: skip
        assert outcomes[TCS.key].exit_reason == "stop" and outcomes[TCS.key].net_pnl < 0
        # B vetoed TCS: its counterfactual says the veto avoided a loss.
        vetoed = {e.payload.signal_id for e in store.read(types=["SignalDisposition"])
                  if e.payload.book_id == "B" and e.payload.disposition == "vetoed"}  # fmt: skip
        assert outcomes[TCS.key].signal_id in vetoed
