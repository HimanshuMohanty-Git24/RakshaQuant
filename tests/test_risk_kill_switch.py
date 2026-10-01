"""Plan M4.4: latching, persisted kill switches; the HALT file; FLATTEN through the OMS."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from src.config.limits import load_risk_limits
from src.domain.clock import ReplayClock
from src.domain.sink import RecordingSink
from src.domain.types import (
    IntentKind,
    IntentReason,
    IntentSource,
    KillScope,
    KillSwitchState,
    OrderStatus,
    Product,
    ReasonCode,
)
from src.oms.exit_manager import ExitPolicy
from src.risk.kill_switch import Flattener, KillSwitchRegistry
from src.risk.monitor import RiskMonitor
from src.risk.state import Breach, DailyRiskTracker
from src.store.kv import KVStateStore, MemoryStateStore
from src.store.sink import StoreSink
from tests.oms_harness import INFY, Harness, at, harness
from tests.test_oms_exit_manager import enter, live_orders, manager

ARMED, HALT_NEW, FLATTEN = KillSwitchState.ARMED, KillSwitchState.HALT_NEW, KillSwitchState.FLATTEN
G, S = KillScope.GLOBAL, KillScope.STRATEGY


def registry(clock, store=None, halt_file=None, **limits):
    sink = RecordingSink(clock)
    reg = KillSwitchRegistry(book_id="A", limits=load_risk_limits(limits or None), clock=clock,
                             sink=sink, state_store=store or MemoryStateStore(),
                             halt_file=halt_file)  # fmt: skip
    return reg, sink


def breach(code=ReasonCode.PF_DAILY_LOSS_MTM, action="FLATTEN", scope=G, name="global") -> Breach:
    return Breach(scope, name, action, code, 0.012, 0.01, "test breach")


def test_switches_latch_and_only_escalate():
    reg, sink = registry(ReplayClock(at(10, 0)))
    assert reg.trip(G, HALT_NEW, reason="r", actor="api")
    assert not reg.trip(G, HALT_NEW, reason="again", actor="api")
    assert reg.trip(G, FLATTEN, reason="worse", actor="monitor")
    assert not reg.trip(G, HALT_NEW, reason="lower", actor="api")  # never de-escalates
    assert reg.state(G) is FLATTEN
    with pytest.raises(ValueError, match="resume"):
        reg.trip(G, ARMED, reason="x", actor="api")
    assert reg.resume(G, actor="operator", reason="checked")
    assert not reg.resume(G, actor="operator", reason="already armed")
    changes = [(e.payload.previous, e.payload.current, e.payload.actor)
               for e in sink.events if e.type == "KillSwitchChanged"]  # fmt: skip
    assert changes == [(ARMED, HALT_NEW, "api"), (HALT_NEW, FLATTEN, "monitor"),
                       (FLATTEN, ARMED, "operator")]  # fmt: skip
    assert [e.payload.level for e in sink.events if e.type == "Alert"] == ["WARNING", "CRITICAL"]


def test_breaches_trip_the_right_switch():
    reg, _ = registry(ReplayClock(at(10, 0)))
    tripped = reg.apply([
        breach(ReasonCode.STR_CONSEC_LOSSES, "HALT_NEW", S, "momentum"),
        breach(ReasonCode.SYS_REJECT_STORM, "HALT_NEW"),
    ])  # fmt: skip
    assert len(tripped) == 2
    state = reg.kill_state()
    assert state.global_state is HALT_NEW and state.strategies == {"momentum": HALT_NEW}
    assert state.broker is ARMED and reg.flattening() == []
    reg.trip(S, FLATTEN, reason="api", actor="api", name="mean_reversion")
    assert reg.flattening() == ["mean_reversion"]
    reg.apply([breach()])
    assert reg.flattening() == [None]


async def test_a_restart_keeps_halt_latched_and_only_rearms_if_configured(tmp_path):
    from src.store.event_store import EventStore

    clock = ReplayClock(at(10, 0))
    with EventStore(tmp_path / "rq.db") as store:
        reg, _ = registry(clock, KVStateStore(store, "A", "kill_switches"))
        reg.apply([breach(action="HALT_NEW")])  # a daily-loss trip
        reg.trip(S, HALT_NEW, reason="streak", actor="monitor", name="momentum",
                 code=ReasonCode.STR_CONSEC_LOSSES.value)  # fmt: skip

    await clock.advance_to(at(9, 0) + timedelta(days=1))
    with EventStore(tmp_path / "rq.db") as store:  # restart on the next IST day
        kv = KVStateStore(store, "A", "kill_switches")
        latched, _ = registry(clock, kv)
        assert latched.state(G) is HALT_NEW and latched.new_day() == []  # not configured
        assert latched.state(G) is HALT_NEW

        rearming, _ = registry(clock, kv, rearm_on_new_day=True)
        assert rearming.new_day() == ["global"]  # the daily-loss trip re-arms...
        assert rearming.state(G) is ARMED
        assert rearming.state(S, "momentum") is HALT_NEW  # ...a loss streak does not


async def test_rearm_never_touches_drawdown_or_same_day_trips():
    clock = ReplayClock(at(10, 0))
    reg, _ = registry(clock, rearm_on_new_day=True)
    reg.apply([breach(ReasonCode.PF_DRAWDOWN)])
    assert reg.new_day() == []  # same day
    await clock.advance_to(at(10, 0) + timedelta(days=1))
    assert reg.new_day() == [] and reg.state(G) is FLATTEN


def test_the_halt_file_trips_and_blocks_resume_until_removed(tmp_path):
    halt = tmp_path / "HALT"
    reg, _ = registry(ReplayClock(at(10, 0)), halt_file=halt)
    assert not reg.check_halt_file()
    halt.write_text("", encoding="utf-8")
    assert reg.check_halt_file() and reg.state(G) is HALT_NEW
    assert reg.record(G).actor == "halt_file"
    halt.write_text("FLATTEN please\n", encoding="utf-8")
    assert reg.check_halt_file() and reg.state(G) is FLATTEN
    with pytest.raises(RuntimeError, match="remove"):
        reg.resume(G, actor="operator", reason="too early")
    halt.unlink()
    assert reg.resume(G, actor="operator", reason="done")


def test_the_settings_point_the_halt_file_at_the_environment(settings):
    assert settings.halt_file == settings.state_dir / "HALT"


# --- FLATTEN through the OMS ------------------------------------------------------------------------


def wire(h: Harness, *, exits=None, halt_file=None, **limits):
    limits_ = load_risk_limits(limits or None)
    sink = StoreSink(h.store, h.clock, "risk")
    tracker = DailyRiskTracker(book_id="A", limits=limits_, clock=h.clock, sink=sink,
                               state_store=KVStateStore(h.store, "A", "daily_risk"))  # fmt: skip
    switches = KillSwitchRegistry(book_id="A", limits=limits_, clock=h.clock, sink=sink,
                                  state_store=KVStateStore(h.store, "A", "kill_switches"),
                                  halt_file=halt_file)  # fmt: skip
    flattener = Flattener(oms=h.oms, clock=h.clock, sink=sink, limits=limits_, exit_manager=exits,
                          state_store=KVStateStore(h.store, "A", "flatten"))  # fmt: skip
    h.oms.add_event_listener(tracker.on_event)

    def marks() -> dict[str, Decimal]:
        return {} if h.last_quote is None else {INFY.key: Decimal(str(h.last_quote.ltp))}

    return RiskMonitor(book=h.book, tracker=tracker, switches=switches, flattener=flattener,
                       marks=marks, clock=h.clock, sink=sink)  # fmt: skip


async def test_acceptance_a_breach_with_no_signals_flattens(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h, ExitPolicy(k_stop_atr=3.0, k_target_atr=6.0))  # stop ~938.5
        monitor = wire(h, exits=exits)
        monitor.start_day()  # at startup, before any order
        await enter(h, exits, qty=500)  # 500 INFY @ ~1000, a resting SL-M stop
        assert [i.split(":")[-1] for i in live_orders(h)] == ["stop-2026-10-05-v1"]

        h.quote(995.0)  # no signal, no decision cycle: only the monitor is watching
        report = await monitor.tick()  # -0.25% of 10L: inside both limits
        assert report.breaches == () and monitor.switches.state(G) is ARMED

        h.quote(978.0)  # -1.1%: the daily-loss limit (FLATTEN by default), above the stop
        report = await monitor.tick()
        assert {b.code for b in report.tripped} == {
            ReasonCode.PF_DAILY_LOSS_MTM,  # -> global FLATTEN
            ReasonCode.STR_DAILY_LOSS,  # -> momentum HALT_NEW (0.5% limit)
        }
        assert monitor.switches.state(G) is FLATTEN and report.flatten_remaining == 1
        flat = [o for o in h.oms.orders.values() if o.intent.kind is IntentKind.FLATTEN]
        assert [o.intent.intent_id.split(":")[-1] for o in flat] == ["flatten-1"]
        assert flat[0].intent.source is IntentSource.KILL_SWITCH and flat[0].intent.reduce_only
        stop = next(o for o in h.oms.orders.values() if o.intent.reason is IntentReason.STOP)
        assert stop.status is OrderStatus.CANCELLED  # the stop was pulled to free the shares

        h.quote(977.0)  # the flatten fills
        report = await monitor.tick()
        assert h.book.quantity(INFY.key, Product.CNC) == 0 and report.flatten_remaining == 0
        (trade,) = h.store.query("SELECT exit_reason FROM trades")
        assert trade["exit_reason"] == IntentReason.FLATTEN.value
        assert monitor.switches.state(G) is FLATTEN  # still latched: resume is manual
        assert exits.positions == {}


async def test_flatten_retries_with_fresh_legs_and_escalates(tmp_path):
    with harness(tmp_path) as h:
        monitor = wire(h)
        monitor.start_day()
        await enter(h, manager(h))
        monitor.switches.trip(G, FLATTEN, reason="api", actor="api")
        await h.clock.advance_to(at(15, 45))  # after the close: every attempt is rejected
        for _ in range(4):
            await monitor.tick()
        legs = sorted(o.intent.intent_id.split(":")[-1] for o in h.oms.orders.values()
                      if o.intent.kind is IntentKind.FLATTEN)  # fmt: skip
        assert legs == ["flatten-1", "flatten-2", "flatten-3", "flatten-4"]
        escalations = [a for a in h.events("Alert") if a.key == "flatten_escalated"]
        assert len(escalations) == 1 and escalations[0].level == "CRITICAL"


async def test_a_working_flatten_is_not_doubled(tmp_path):
    with harness(tmp_path) as h:
        monitor = wire(h)
        monitor.start_day()
        await enter(h, manager(h))
        monitor.switches.trip(G, FLATTEN, reason="api", actor="api")
        await monitor.tick()
        await monitor.tick()  # no quote since: the first flatten is still working
        flat = [o for o in h.oms.orders.values() if o.intent.kind is IntentKind.FLATTEN]
        assert len(flat) == 1 and not flat[0].status.is_terminal


async def test_strategy_flatten_only_touches_that_strategy(tmp_path):
    with harness(tmp_path) as h:
        monitor = wire(h)
        monitor.start_day()
        await enter(h, manager(h))
        monitor.switches.trip(S, FLATTEN, reason="api", actor="api", name="mean_reversion")
        report = await monitor.tick()
        assert report.flatten_remaining == 0 and h.book.quantity(INFY.key, Product.CNC) == 100
        monitor.switches.trip(S, FLATTEN, reason="api", actor="api", name="momentum")
        assert (await monitor.tick()).flatten_remaining == 1


async def test_the_halt_file_is_seen_on_the_next_tick(tmp_path):
    with harness(tmp_path) as h:
        halt = tmp_path / "HALT"
        monitor = wire(h, halt_file=halt)
        await h.oms.start()
        h.quote(1000.0)
        monitor.start_day()
        halt.touch()
        report = await monitor.tick()
        assert report.halt_file and monitor.switches.kill_state().global_state is HALT_NEW


async def test_the_monitor_loop_survives_a_failing_tick(tmp_path):
    with harness(tmp_path) as h:
        monitor = wire(h)
        calls = 0

        async def boom() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("bug")

        monitor.tick = boom  # type: ignore[method-assign]
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        await h.clock.advance(60)
        await h.clock.advance(60)
        stop.set()
        await h.clock.advance(60)
        await task
        assert calls == 3
        assert [a.key for a in h.events("Alert")] == ["risk_tick_failed"] * 3


async def test_a_held_instrument_without_a_quote_is_valued_at_cost(tmp_path):
    with harness(tmp_path) as h:
        monitor = wire(h)
        await enter(h, manager(h))
        h.last_quote = None  # the feed has nothing for it
        marks = monitor.marks()
        assert marks[INFY.key] == h.book.position(INFY.key, Product.CNC, h.clock.now()).avg_price
        monitor.marks()
        assert [a.key for a in h.events("Alert")].count("risk_no_mark") == 1


async def test_reconcile_takes_back_a_stood_down_position(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h)
        await enter(h, exits)
        await exits.stand_down(INFY.key, Product.CNC)
        assert live_orders(h) == [] and exits.positions[f"{INFY.key}|CNC"].closing
        notes = await exits.reconcile(atr={})
        assert any("managing it again" in n for n in notes)
        assert [i.split(":")[-1] for i in live_orders(h)] == ["stop-2026-10-05-v2"]
