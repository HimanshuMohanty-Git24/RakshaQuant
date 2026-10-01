"""Plan M3.5: exits are reduce-only, anchored to the fill, decrement on partials (audit F-01)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.brokers.simulated.costs import NSECostSchedule
from src.domain.calendar import get_calendar
from src.domain.types import IntentReason, OrderStatus, OrderType, Product, Side
from src.oms.exit_manager import ExitManager, ExitPolicy
from src.store.kv import KVStateStore
from src.store.sink import StoreSink
from tests.oms_harness import INFY, Harness, at, harness, intent

ATR = Decimal("20.5")
F01_POLICY = ExitPolicy(
    k_stop_atr=1.0, k_target_atr=2.0, k_trail_atr=None, partial_at_r=1.0, partial_fraction=0.5
)


def manager(h: Harness, policy: ExitPolicy = F01_POLICY, store_state: bool = False) -> ExitManager:
    return ExitManager(
        oms=h.oms,
        clock=h.clock,
        calendar=get_calendar(),
        sink=StoreSink(h.store, h.clock, "exit_manager"),
        policy=policy,
        state_store=KVStateStore(h.store, "A", "exit_manager") if store_state else None,
    )


async def tick(h: Harness, exits: ExitManager, price: float) -> None:
    h.quote(price)
    assert h.last_quote is not None
    await exits.on_quote(h.last_quote)


async def enter(h: Harness, exits: ExitManager, qty: int = 100, price: float = 1000.0) -> None:
    await h.oms.start()
    h.quote(price)
    entry = intent(Side.BUY, qty)
    exits.expect_entry(entry, atr=ATR, signal_bar_date=date(2026, 10, 2))
    await h.oms.submit(entry)
    await tick(h, exits, price)  # fills the entry; the stop goes in on this tick


def live_orders(h: Harness) -> list[str]:
    return [o.intent.intent_id for o in h.oms.orders.values() if not o.status.is_terminal]


async def test_audit_f01_partial_then_full_exit_ends_flat_with_consistent_pnl(tmp_path):
    with harness(tmp_path, costs=NSECostSchedule.from_yaml()) as h:
        exits = manager(h)
        await enter(h, exits)
        pos = exits.positions[f"{INFY.key}|CNC"]
        fill = pos.entry_price  # 1000.05: the fill, not the 1000 decision price
        assert pos.stop_price == fill - ATR and pos.target_price == fill + 2 * ATR
        assert [i.split(":")[-1] for i in live_orders(h)] == ["stop-2026-10-05-v1"]

        await tick(h, exits, 1021.0)  # 1R: partial of 50 at market (the stop is pulled first)
        await tick(h, exits, 1022.0)  # the partial fills; the remaining 50 get a new stop
        pos = exits.positions[f"{INFY.key}|CNC"]
        assert pos.quantity == 50 and pos.partial_taken
        assert h.book.quantity(INFY.key, Product.CNC) == 50
        stop = h.oms.orders[pos.stop_coid]  # type: ignore[index]
        assert stop.quantity == 50 and stop.intent.order_type is OrderType.SL_M

        await tick(h, exits, 1045.0)  # target: the rest at market
        await tick(h, exits, 1046.0)
        assert h.book.quantity(INFY.key, Product.CNC) == 0  # flat: no phantom short
        assert exits.positions == {} and live_orders(h) == []
        (broker_pos,) = await h.broker.get_positions()
        assert broker_pos.quantity == 0

        trades = h.store.query("SELECT quantity, exit_reason, net_pnl FROM trades ORDER BY seq")
        assert [(t["quantity"], t["exit_reason"]) for t in trades] == [
            (50, "partial"),
            (50, "target"),
        ]
        dashboard = sum((Decimal(t["net_pnl"]) for t in trades), Decimal(0))
        assert dashboard == h.book.realized_pnl == broker_pos.realized_pnl
        assert dashboard > 0


async def test_stop_and_target_are_anchored_to_the_fill_not_the_decision(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h, ExitPolicy(k_stop_atr=2.0, k_target_atr=3.0, k_trail_atr=None))
        await h.oms.start()
        h.quote(1000.0)
        entry = intent(Side.BUY, 10)  # decided at 1000...
        exits.expect_entry(entry, atr=Decimal(10), signal_bar_date=date(2026, 10, 2))
        await h.oms.submit(entry)
        await tick(h, exits, 1030.0)  # ...but it filled after a 3% move
        pos = exits.positions[f"{INFY.key}|CNC"]
        assert pos.entry_price == Decimal("1030.15")  # 1030 + 1 bp, rounded up to the tick
        assert (pos.stop_price, pos.target_price) == (Decimal("1010.15"), Decimal("1060.15"))


async def test_a_gap_through_the_stop_fills_at_the_open(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h, ExitPolicy(k_stop_atr=1.0, k_target_atr=3.0, k_trail_atr=None))
        await enter(h, exits)
        h.broker.expire_session(at(15, 30))  # the DAY stop expires overnight

        day2 = date(2026, 10, 6)
        await h.clock.advance_to(at(9, 15, 5, day=day2))
        await exits.on_session_start(day2, atr={INFY.key: ATR}, closes={INFY.key: Decimal(1000)})
        await tick(h, exits, 900.0)  # the first quote of the day gaps far below the 979.55 stop
        (trade,) = h.store.query("SELECT exit_price, exit_reason FROM trades")
        assert trade["exit_reason"] == "stop"
        # The open (900) less the 1 bp spread, rounded down to the tick: not the 979.55 trigger.
        assert Decimal(trade["exit_price"]) == Decimal("899.90")
        assert h.book.quantity(INFY.key, Product.CNC) == 0 and exits.positions == {}


async def test_time_exit_after_max_hold_days(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h, ExitPolicy(max_hold_days=2, k_trail_atr=None))
        await enter(h, exits)
        for day in (date(2026, 10, 6), date(2026, 10, 7)):
            h.broker.expire_session(at(15, 30, day=h.clock.now().date()))
            await h.clock.advance_to(at(9, 15, 5, day=day))
            await exits.on_session_start(day, atr={}, closes={})
        await tick(h, exits, 1001.0)
        (trade,) = h.store.query("SELECT exit_reason FROM trades")
        assert trade["exit_reason"] == "time"


async def test_trailing_stop_only_ratchets_up(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h, ExitPolicy(k_stop_atr=2.0, k_trail_atr=2.0))
        await enter(h, exits)
        key = f"{INFY.key}|CNC"
        initial = exits.positions[key].stop_price
        h.broker.expire_session(at(15, 30))
        await h.clock.advance_to(at(9, 15, 5, day=date(2026, 10, 6)))
        await exits.on_session_start(
            date(2026, 10, 6), atr={INFY.key: ATR}, closes={INFY.key: Decimal(1100)}
        )
        raised = exits.positions[key].stop_price
        assert raised == Decimal(1100) - 2 * ATR > initial
        h.broker.expire_session(at(15, 30, day=date(2026, 10, 6)))
        await h.clock.advance_to(at(9, 15, 5, day=date(2026, 10, 7)))
        await exits.on_session_start(
            date(2026, 10, 7), atr={INFY.key: ATR}, closes={INFY.key: Decimal(1000)}
        )
        assert exits.positions[key].stop_price == raised  # never loosened
        stop = h.oms.orders[exits.positions[key].stop_coid]  # type: ignore[index]
        assert stop.intent.trigger_price == raised and stop.intent.intent_id.endswith(
            "2026-10-07-v3"
        )


async def test_flatten_exits_everything_reduce_only(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h)
        await enter(h, exits)
        await exits.flatten()
        await tick(h, exits, 1000.0)
        assert h.book.quantity(INFY.key, Product.CNC) == 0 and live_orders(h) == []
        (trade,) = h.store.query("SELECT exit_reason FROM trades")
        assert trade["exit_reason"] == IntentReason.FLATTEN.value


async def test_a_one_share_position_keeps_its_stop_instead_of_a_zero_partial(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h)
        await enter(h, exits, qty=1)
        await tick(h, exits, 1021.0)  # 1R reached, but half of 1 share is 0
        pos = exits.positions[f"{INFY.key}|CNC"]
        assert pos.partial_taken and pos.quantity == 1
        assert not [o for o in h.oms.orders.values() if o.intent.reason is IntentReason.PARTIAL]


async def test_state_survives_restart_and_reconcile_adopts_or_drops(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h, store_state=True)
        await enter(h, exits)
        reborn = manager(h, store_state=True)
        assert reborn.positions == exits.positions  # restored from the kv store

        # A position the exit manager does not know: adopted with ATR-based exits.
        reborn._state.positions.clear()
        notes = await reborn.reconcile(atr={INFY.key: ATR})
        assert any(n.startswith("adopted") for n in notes)
        assert reborn.positions[f"{INFY.key}|CNC"].quantity == 100
        assert [a.key for a in h.events("Alert")] == ["exit_reconcile"]

        # A managed entry the book no longer holds: dropped.
        ghost = reborn.positions[f"{INFY.key}|CNC"]
        await reborn.flatten()
        await tick(h, reborn, 1000.0)
        reborn._state.positions[ghost.key] = ghost
        notes = await reborn.reconcile(atr={})
        assert any(n.startswith("dropped") for n in notes) and reborn.positions == {}


async def test_exit_fills_never_short_the_book(tmp_path):
    with harness(tmp_path) as h:
        exits = manager(h)
        await enter(h, exits)
        await exits.flatten()
        await exits.flatten()  # a second flatten is a duplicate intent, not a second sell
        await tick(h, exits, 1000.0)
        assert h.book.quantity(INFY.key, Product.CNC) == 0
        sells = [
            o
            for o in h.oms.orders.values()
            if o.intent.side is Side.SELL and o.status is OrderStatus.FILLED
        ]
        assert sum(o.filled_qty for o in sells) == 100
