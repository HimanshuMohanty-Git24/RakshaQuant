"""Plan M4.3: the persisted per-IST-day risk state and the monitor's risk tick."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.config.limits import load_risk_limits
from src.domain.clock import ReplayClock
from src.domain.events import OrderRejected, OrderSubmitted, TradeClosed
from src.domain.sink import RecordingSink
from src.domain.types import KillScope, Order, OrderStatus, ReasonCode, Side
from src.risk.state import DailyRiskTracker
from src.store.event_store import EventStore
from src.store.kv import KVStateStore, MemoryStateStore
from tests.oms_harness import at, harness
from tests.oms_harness import intent as oms_intent

R = ReasonCode
LAKH = Decimal(100_000)
TEN_LAKH = Decimal(1_000_000)


def tracker(clock: ReplayClock, store: MemoryStateStore | KVStateStore | None = None, **limits):
    sink = RecordingSink(clock)
    t = DailyRiskTracker(book_id="A", limits=load_risk_limits(limits or None), clock=clock,
                         sink=sink, state_store=store or MemoryStateStore())  # fmt: skip
    return t, sink


def trade(net: str, *, exit_id: str = "x1", strategy: str = "momentum", key: str = "NSE:EQ:INFY",
          trade_id: str = "t1") -> TradeClosed:  # fmt: skip
    return TradeClosed(
        trade_id=trade_id, book_id="A", decision_id="d1", exit_decision_id=exit_id,
        instrument_key=key, strategy=strategy, side=Side.BUY, quantity=10,
        entry_price=Decimal(100), exit_price=Decimal(90),
        entry_ts=datetime(2026, 10, 5, 4, tzinfo=UTC), exit_ts=datetime(2026, 10, 5, 5, tzinfo=UTC),
        gross_pnl=Decimal(net), charges=Decimal(0), net_pnl=Decimal(net), exit_reason="stop",
    )  # fmt: skip


def submitted(*, opening: bool = True) -> OrderSubmitted:
    side = Side.BUY if opening else Side.SELL
    i = oms_intent(side, 10, leg="entry" if opening else "exit")
    order = Order(client_order_id="c1", intent=i, quantity=10, status=OrderStatus.SUBMITTED,
                  submitted_at=at(9, 30))  # fmt: skip
    return OrderSubmitted(order=order)


def test_start_day_seeds_and_a_restart_keeps_the_day(tmp_path):
    clock = ReplayClock(at(9, 0))
    with EventStore(tmp_path / "rq.db") as store:
        kv = KVStateStore(store, "A", "daily_risk")
        t, _ = tracker(clock, kv)
        t.start_day(TEN_LAKH)
        assert t.risk_tick(Decimal(985_000))[0].code is R.PF_DAILY_LOSS_MTM  # -1.5%
        t.record_trade(trade("-500"))

    with EventStore(tmp_path / "rq.db") as store:  # the process restarts
        again, _ = tracker(clock, KVStateStore(store, "A", "daily_risk"))
        state = again.start_day(Decimal(985_000))  # a later "SOD" must not reset the day
        assert state.sod_equity == TEN_LAKH and state.realized_pnl == Decimal(-500)
        assert again.risk_tick(Decimal(984_000)) == []  # the breach is remembered, not re-raised
        assert "PF_DAILY_LOSS_MTM:global" in state.breaches


def test_daily_loss_is_measured_mark_to_market_against_start_of_day():
    clock = ReplayClock(at(9, 0))
    t, sink = tracker(clock)
    t.start_day(TEN_LAKH)
    assert t.risk_tick(Decimal(990_001)) == []  # -0.9999%: inside the 1% limit
    [breach] = t.risk_tick(Decimal(990_000))
    assert (breach.scope, breach.name, breach.action) == (KillScope.GLOBAL, "global", "FLATTEN")
    assert breach.observed == pytest.approx(0.01)
    assert [e.payload.code for e in sink.events if e.type == "LimitBreached"] == [
        R.PF_DAILY_LOSS_MTM
    ]
    assert t.risk_tick(Decimal(980_000)) == []  # once per IST day


def test_the_action_comes_from_the_limits():
    clock = ReplayClock(at(9, 0))
    t, _ = tracker(clock, daily_loss_action="HALT_NEW")
    t.start_day(TEN_LAKH)
    assert t.risk_tick(Decimal(980_000))[0].action == "HALT_NEW"


async def test_drawdown_runs_from_a_peak_that_survives_the_day_roll():
    clock = ReplayClock(at(15, 0))
    t, sink = tracker(clock)
    t.start_day(TEN_LAKH)
    assert t.risk_tick(Decimal(1_100_000)) == []  # new peak, +10%
    await clock.advance_to(at(9, 0) + timedelta(days=1))
    day2 = t.start_day(Decimal(1_100_000))
    assert day2.peak_equity == Decimal(1_100_000)
    assert t.risk_tick(Decimal(1_095_000)) == []
    await clock.advance_to(at(9, 0) + timedelta(days=2))
    t.start_day(Decimal(1_050_000))  # day 3 opens -4.5% below the peak; no single day lost 1%
    [breach] = t.risk_tick(Decimal(1_044_000))  # -0.57% today, -5.09% from the peak
    assert breach.code is R.PF_DRAWDOWN and breach.observed == pytest.approx(0.050909, abs=1e-6)
    rolled = [e.payload for e in sink.events if e.type == "DailyRiskStateRolled"]
    assert [r.ist_date.isoformat() for r in rolled] == ["2026-10-05", "2026-10-06"]


async def test_a_new_day_resets_counters_but_keeps_loss_streaks():
    clock = ReplayClock(at(9, 0))
    t, _ = tracker(clock)
    t.start_day(TEN_LAKH)
    t.on_event(submitted())
    t.on_event(trade("-100", exit_id="x1"))
    t.on_event(trade("-100", exit_id="x2"))
    assert t.state is not None and t.state.entries == 1
    await clock.advance_to(at(9, 0) + timedelta(days=1))
    day2 = t.start_day(TEN_LAKH)
    assert day2.entries == 0 and day2.realized_pnl == 0 and not day2.symbols_exited
    assert day2.strategies["momentum"].consecutive_losses == 2
    assert day2.strategies["momentum"].realized_pnl == 0


async def test_a_tick_without_start_day_rolls_from_the_last_mark():
    clock = ReplayClock(at(15, 0))
    t, _ = tracker(clock)
    t.start_day(TEN_LAKH)
    t.risk_tick(Decimal(995_000))
    await clock.advance_to(at(10, 0) + timedelta(days=1))
    t.on_event(submitted())  # the first event of the new day rolls it
    assert t.state is not None and t.state.sod_equity == Decimal(995_000)


def test_events_need_a_started_day():
    t, _ = tracker(ReplayClock(at(9, 0)))
    with pytest.raises(RuntimeError, match="start_day"):
        t.record_reject()


def test_streaks_count_exits_not_lots_and_trip_the_strategy_switch():
    clock = ReplayClock(at(9, 0))
    t, _ = tracker(clock)
    t.start_day(TEN_LAKH)
    for n in range(3):  # one exit that closed two FIFO lots is one loss
        t.record_trade(trade("-10", exit_id=f"x{n}", trade_id=f"t{n}a"))
        t.record_trade(trade("-10", exit_id=f"x{n}", trade_id=f"t{n}b"))
    assert t.state is not None and t.state.strategies["momentum"].consecutive_losses == 3
    assert t.risk_tick(TEN_LAKH) == []
    t.record_trade(trade("-10", exit_id="x4"))
    [breach] = t.risk_tick(TEN_LAKH)
    assert (breach.scope, breach.name, breach.code) == (
        KillScope.STRATEGY, "momentum", R.STR_CONSEC_LOSSES,
    )  # fmt: skip
    assert breach.action == "HALT_NEW"
    t.record_trade(trade("+50", exit_id="x5"))
    assert t.state.strategies["momentum"].consecutive_losses == 0


def test_strategy_daily_loss_includes_open_positions():
    clock = ReplayClock(at(9, 0))
    t, _ = tracker(clock)
    t.start_day(TEN_LAKH)
    t.record_trade(trade("-3000", strategy="mean_reversion"))
    assert t.risk_tick(TEN_LAKH, unrealized_by_strategy={"mean_reversion": Decimal(-1999)}) == []
    [breach] = t.risk_tick(TEN_LAKH, unrealized_by_strategy={"mean_reversion": Decimal(-2000)})
    assert (breach.name, breach.code) == ("mean_reversion", R.STR_DAILY_LOSS)  # 0.5% of 10L
    stats = t.strategy_stats({"mean_reversion": Decimal(-2000)})
    assert stats["mean_reversion"].day_pnl == Decimal(-5000)


async def test_reject_storm_halts_new_orders():
    clock = ReplayClock(at(9, 0))
    t, _ = tracker(clock)
    t.start_day(TEN_LAKH)
    for _ in range(4):
        t.on_event(OrderRejected(client_order_id="c", book_id="A", decision_id="d",
                                 instrument_key="NSE:EQ:INFY", reason="X"))  # fmt: skip
    assert t.rejects_in_window() == 4 and t.risk_tick(TEN_LAKH) == []
    await clock.advance_to(at(9, 11))  # the first four age out of the 10-minute window
    t.record_reject()
    assert t.rejects_in_window() == 1
    for _ in range(4):
        t.record_reject()
    [breach] = t.risk_tick(TEN_LAKH)
    assert (breach.code, breach.action) == (R.SYS_REJECT_STORM, "HALT_NEW")


async def test_order_rates_use_a_sliding_minute():
    clock = ReplayClock(at(9, 30))
    t, _ = tracker(clock)
    t.start_day(TEN_LAKH)
    t.on_event(submitted())
    t.on_event(submitted(opening=False))
    assert t.orders_last_min() == 2 and t.strategy_stats()["momentum"].orders_last_min == 2
    assert t.state is not None and t.state.entries == 1
    assert t.state.symbols_entered == {"NSE:EQ:INFY"}
    await clock.advance_to(at(9, 31))
    assert t.orders_last_min() == 0


def test_a_state_for_another_book_is_refused():
    clock = ReplayClock(at(9, 0))
    store = MemoryStateStore()
    t, _ = tracker(clock, store)
    t.start_day(TEN_LAKH)
    with pytest.raises(ValueError, match="book A"):
        DailyRiskTracker(book_id="B", limits=load_risk_limits(), clock=clock,
                         sink=RecordingSink(clock), state_store=store)  # fmt: skip


async def test_the_oms_feeds_the_risk_state(tmp_path):
    with harness(tmp_path) as h:
        t, _ = tracker(h.clock)
        t.start_day(TEN_LAKH)
        h.oms.add_event_listener(t.on_event)
        await h.oms.start()
        h.quote(1000.0)
        await h.oms.submit(oms_intent(Side.BUY, 10))
        h.quote(1000.0)
        await h.oms.submit(oms_intent(Side.SELL, 10, leg="exit", decision_id="d2"))
        h.quote(990.0)
        state = t.state
        assert state is not None and state.entries == 1 and len(state.order_ts) == 2
        assert state.realized_pnl < 0 and state.symbols_exited == {"NSE:EQ:INFY"}
        assert state.strategies["momentum"].consecutive_losses == 1


async def test_a_failing_listener_never_breaks_the_oms(tmp_path):
    def broken(payload: object) -> None:
        raise RuntimeError("bug")

    with harness(tmp_path) as h:
        h.oms.add_event_listener(broken)
        await h.oms.start()
        h.quote(1000.0)
        result = await h.oms.submit(oms_intent(Side.BUY, 10))
        assert result.status == "SUBMITTED"
        assert any(a.key == "oms_listener_failed" for a in h.events("Alert"))


async def test_an_operator_resume_acknowledges_the_losing_streak():
    """Without it the streak (reset only by a win) re-trips the strategy every morning."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from src.config.limits import load_risk_limits
    from src.domain.clock import ReplayClock
    from src.domain.events import TradeClosed
    from src.domain.sink import RecordingSink
    from src.domain.types import KillScope, KillSwitchState, Side
    from src.engine.runner import acknowledging
    from src.risk.kill_switch import KillSwitchRegistry
    from src.risk.state import DailyRiskTracker

    clock = ReplayClock(datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    sink = RecordingSink(clock)
    limits = load_risk_limits()
    tracker = DailyRiskTracker(book_id="A", limits=limits, clock=clock, sink=sink)
    switches = KillSwitchRegistry(book_id="A", limits=limits, clock=clock, sink=sink,
                                  on_resume=acknowledging(tracker))  # fmt: skip
    tracker.start_day(Decimal(1_000_000))
    for i in range(limits.strategy_max_consec_losses):
        tracker.record_trade(TradeClosed(
            trade_id=f"t{i}", book_id="A", decision_id=f"d{i}", instrument_key="NSE:EQ:INFY",
            strategy="momentum", side=Side.BUY, quantity=1, entry_price=Decimal(100),
            exit_price=Decimal(99), entry_ts=clock.now(), exit_ts=clock.now(),
            gross_pnl=Decimal(-1), charges=Decimal(0), net_pnl=Decimal(-1), exit_reason="stop"))  # fmt: skip
    assert switches.apply(tracker.risk_tick(Decimal(999_995)))
    assert switches.state(KillScope.STRATEGY, "momentum") is KillSwitchState.HALT_NEW

    assert switches.resume(KillScope.STRATEGY, name="momentum", actor="web", reason="reviewed")
    await clock.advance(timedelta(days=1).total_seconds())  # the next session
    tracker.start_day(Decimal(999_995))
    assert not switches.apply(tracker.risk_tick(Decimal(999_995)))  # not re-tripped
    assert switches.state(KillScope.STRATEGY, "momentum") is KillSwitchState.ARMED
