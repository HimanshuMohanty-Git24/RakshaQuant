"""Plan M3.4: the OMS (single submit path, state machine, UNKNOWN, reconcile) and PositionBook."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.brokers.base import BrokerAck, TransportError
from src.brokers.simulated.costs import NSECostSchedule
from src.brokers.simulated.fill_model import FillModelConfig, MarketContext
from src.domain.types import Fill, IntentKind, Order, OrderStatus, OrderType, Product, Side
from src.oms.oms import GateResult
from src.oms.position_book import PositionBook
from tests.oms_harness import INFY, harness, intent

T = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def fill(fid: str, side: Side, qty: int, price: str, charges: str = "0") -> Fill:
    return Fill(
        fill_id=fid,
        client_order_id="c",
        book_id="A",
        decision_id="d",
        instrument_key=INFY.key,
        side=side,
        quantity=qty,
        price=Decimal(price),
        ts=T,
        charges=Decimal(charges),
    )


# --- PositionBook ---------------------------------------------------------------------------


def test_hedged_book_nets_to_one_position():
    book = PositionBook("A", Decimal(100_000))
    book.apply(fill("f1", Side.BUY, 10, "100"), product=Product.CNC, strategy="s")
    book.apply(fill("f2", Side.BUY, 10, "110"), product=Product.CNC, strategy="s")
    out = book.apply(fill("f3", Side.SELL, 20, "120"), product=Product.CNC, strategy="s")
    assert out.position.quantity == 0 and out.position.avg_price is None
    assert [p.quantity for p in book.positions(T)] == [0]  # flat: not "long 10 and short 10"
    # FIFO: 10 @100 then 10 @110 against 120.
    assert [(c.entry_price, c.net_pnl) for c in out.closed] == [
        (Decimal("100"), Decimal("200")),
        (Decimal("110"), Decimal("100")),
    ]


def test_realized_pnl_is_net_of_both_legs_charges_and_fills_apply_once():
    book = PositionBook("A", Decimal(100_000))
    book.apply(
        fill("f1", Side.BUY, 100, "1000", charges="118.74"), product=Product.CNC, strategy="s"
    )
    out = book.apply(
        fill("f2", Side.SELL, 40, "1010", charges="40.00"), product=Product.CNC, strategy="s"
    )
    (piece,) = out.closed
    # gross 40 x 10 = 400; 40% of the entry charges (47.496) and all exit charges (40).
    assert piece.gross_pnl == Decimal(400) and piece.charges == Decimal("87.496")
    assert piece.net_pnl == Decimal("312.504")
    again = book.apply(
        fill("f2", Side.SELL, 40, "1010", charges="40.00"), product=Product.CNC, strategy="s"
    )
    assert again.duplicate and book.quantity(INFY.key, Product.CNC) == 60


def test_book_refuses_another_books_fill():
    with pytest.raises(ValueError, match="book"):
        PositionBook("B", Decimal(1)).apply(
            fill("f", Side.BUY, 1, "1"), product=Product.CNC, strategy="s"
        )


@st.composite
def _fills(draw: st.DrawFn) -> list[Fill]:
    out = []
    for n in range(draw(st.integers(1, 25))):
        out.append(
            fill(
                f"f{n}",
                draw(st.sampled_from([Side.BUY, Side.SELL])),
                draw(st.integers(1, 50)),
                str(draw(st.integers(9000, 11000)) / 10),
                str(draw(st.integers(0, 3000)) / 100),
            )
        )
    return out


@given(fills=_fills(), mark=st.integers(9000, 11000))
@settings(max_examples=150, deadline=None)
def test_property_cash_and_marks_reconcile_with_pnl(fills, mark):
    """equity = cash + qty x mark, and equity - start = realized + open lots' (mark - price) x qty
    - their unallocated charges: nothing is created or lost by netting, FIFO or flips."""
    start = Decimal(1_000_000)
    book = PositionBook("A", start)
    for f in fills:
        book.apply(f, product=Product.CNC, strategy="s")
    m = Decimal(mark) / 10
    qty = book.quantity(INFY.key, Product.CNC)
    assert book.equity({INFY.key: m}) == book.cash + qty * m
    sign = 1 if qty > 0 else -1
    open_part = sum(
        (
            (m - lot.price) * lot.quantity * sign - lot.charges
            for lot in book.lots(INFY.key, Product.CNC)
        ),
        Decimal(0),
    )
    assert book.equity({INFY.key: m}) - start == book.realized_pnl + open_part


# --- OMS submission ------------------------------------------------------------------------------


async def test_order_is_persisted_before_the_broker_call(tmp_path):
    with harness(tmp_path) as h:
        seen_before_call: list[bool] = []
        real = h.broker.place_order

        async def spy(order: Order) -> BrokerAck:
            stored = [e.payload for e in h.store.read(types=["OrderSubmitted"])]
            seen_before_call.append(
                any(p.order.client_order_id == order.client_order_id for p in stored)
            )
            return await real(order)

        h.broker.place_order = spy  # type: ignore[method-assign]
        await h.oms.start()
        h.quote(1000.0)
        result = await h.oms.submit(intent(Side.BUY, 100))
        assert result.status == "SUBMITTED" and seen_before_call == [True]


async def test_entry_to_fill_lifecycle_and_projections(tmp_path):
    with harness(tmp_path) as h:
        await h.oms.start()
        h.quote(1000.0)
        result = await h.oms.submit(intent(Side.BUY, 100))
        assert result.order is not None and result.order.status is OrderStatus.OPEN
        h.quote(1002.0)
        order = h.oms.orders[result.order.client_order_id]
        assert order.status is OrderStatus.FILLED and order.filled_qty == 100
        assert h.book.quantity(INFY.key, Product.CNC) == 100
        (row,) = h.store.query("SELECT status, filled_qty FROM orders")
        assert (row["status"], row["filled_qty"]) == ("FILLED", 100)
        (pos,) = h.store.query("SELECT quantity FROM positions")
        assert pos["quantity"] == 100
        assert [e.type for e in h.store.read()] == [
            "OrderSubmitted", "OrderAcked", "FillReceived", "PositionChanged",
        ]  # fmt: skip


async def test_a_repeated_intent_is_never_placed_twice(tmp_path):
    with harness(tmp_path) as h:
        await h.oms.start()
        h.quote(1000.0)
        first = await h.oms.submit(intent(Side.BUY, 100))
        again = await h.oms.submit(intent(Side.BUY, 100))
        assert first.status == "SUBMITTED" and again.status == "DUPLICATE"
        assert len(await h.broker.get_orders()) == 1


async def test_a_cnc_short_is_rejected(tmp_path):
    with harness(tmp_path) as h:
        await h.oms.start()
        h.quote(1000.0)
        result = await h.oms.submit(intent(Side.SELL, 10, kind=IntentKind.OPEN))
        assert result.status == "REJECTED" and "SHORT_NOT_ALLOWED" in result.message
        (event,) = h.events("OrderRejected")
        assert event.reason == "SHORT_NOT_ALLOWED"
        assert h.oms.orders[result.order.client_order_id].status is OrderStatus.REJECTED  # type: ignore[union-attr]


async def test_reduce_only_is_clipped_and_never_oversells(tmp_path):
    with harness(tmp_path) as h:
        await h.oms.start()
        h.quote(1000.0)
        await h.oms.submit(intent(Side.BUY, 100))
        h.quote(1000.0)
        stop = await h.oms.submit(
            intent(
                Side.SELL,
                150,
                leg="stop",
                order_type=OrderType.SL_M,
                trigger="900.00",
            )
        )
        assert stop.order is not None and stop.order.quantity == 100  # clipped to the position
        blocked = await h.oms.submit(intent(Side.SELL, 10, leg="target"))
        assert blocked.status == "BLOCKED"  # the resting stop already commits all 100
        await h.oms.cancel(stop.order.client_order_id)
        target = await h.oms.submit(intent(Side.SELL, 10, leg="target2"))
        assert (
            target.status == "SUBMITTED"
            and target.order is not None
            and target.order.quantity == 10
        )


async def test_reduce_only_on_a_flat_book_is_blocked(tmp_path):
    with harness(tmp_path) as h:
        result = await h.oms.submit(intent(Side.SELL, 10, leg="exit"))
        assert result.status == "BLOCKED" and "reduce-only" in result.message


@given(position=st.integers(0, 200), wants=st.lists(st.integers(1, 300), min_size=1, max_size=6))
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_reduce_only_never_flips(position, wants):
    import asyncio

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp, harness(Path(tmp)) as h:
            await h.oms.start()
            h.quote(1000.0)
            if position:
                await h.oms.submit(intent(Side.BUY, position))
                h.quote(1000.0)
            for n, want in enumerate(wants):
                await h.oms.submit(intent(Side.SELL, want, leg=f"exit-{n}"))
                h.quote(1000.0)
                assert h.book.quantity(INFY.key, Product.CNC) >= 0  # never short
            assert h.book.quantity(INFY.key, Product.CNC) == max(0, position - sum(wants))

    asyncio.run(run())


async def test_the_gate_can_resize_or_reject(tmp_path):
    async def gate(i: Any, qty: int | None) -> GateResult:
        return GateResult(quantity=13) if i.side is Side.BUY else GateResult(0, "nope")

    with harness(tmp_path) as h:
        h.oms.use_gate(gate)  # production installs the RiskGate
        h.quote(1000.0)
        resized = await h.oms.submit(intent(Side.BUY, None))
        assert resized.order is not None and resized.order.quantity == 13


async def test_an_unsized_intent_is_blocked(tmp_path):
    with harness(tmp_path) as h:
        assert (await h.oms.submit(intent(Side.BUY, None))).status == "BLOCKED"


async def test_an_oms_without_a_gate_routes_nothing(tmp_path):
    with harness(tmp_path, gate=None) as h:
        await h.oms.start()
        h.quote(1000.0)
        result = await h.oms.submit(intent(Side.BUY, 10))
        assert result.status == "BLOCKED" and "no risk gate" in result.message
        assert h.events("OrderSubmitted") == []


# --- UNKNOWN outcomes ------------------------------------------------------------------------------


async def test_unknown_outcome_is_resolved_by_tag_when_the_order_exists(tmp_path):
    with harness(tmp_path) as h:
        await h.oms.start()
        h.quote(1000.0)
        real = h.broker.place_order

        async def lost_ack(order: Order) -> BrokerAck:
            await real(order)  # the order reaches the exchange...
            raise TransportError("read timeout")  # ...but the response is lost

        h.broker.place_order = lost_ack  # type: ignore[method-assign]
        result = await h.oms.submit(intent(Side.BUY, 100))
        assert result.status == "UNKNOWN"
        await h.oms.resolve_unknown()
        order = h.oms.orders[result.order.client_order_id]  # type: ignore[union-attr]
        assert order.status is OrderStatus.OPEN and order.broker_order_id is not None
        h.quote(1001.0)
        assert h.book.quantity(INFY.key, Product.CNC) == 100


async def test_unknown_order_absent_after_two_checks_and_timeout_is_rejected(tmp_path):
    with harness(tmp_path) as h:

        async def lost(order: Order) -> BrokerAck:
            raise TransportError("connection reset before send")

        h.broker.place_order = lost  # type: ignore[method-assign]
        h.quote(1000.0)
        result = await h.oms.submit(intent(Side.BUY, 100))
        coid = result.order.client_order_id  # type: ignore[union-attr]
        await h.oms.resolve_unknown()
        assert h.oms.orders[coid].status is OrderStatus.UNKNOWN  # one check is not enough
        await h.clock.advance(31)
        await h.oms.resolve_unknown()
        assert h.oms.orders[coid].status is OrderStatus.REJECTED
        assert h.events("OrderRejected")[0].reason == "NOT_FOUND_AT_BROKER"


async def test_unexpected_errors_are_treated_as_unknown_not_rejected(tmp_path):
    with harness(tmp_path) as h:

        async def boom(order: Order) -> BrokerAck:
            raise RuntimeError("adapter bug")

        h.broker.place_order = boom  # type: ignore[method-assign]
        h.quote(1000.0)
        assert (await h.oms.submit(intent(Side.BUY, 100))).status == "UNKNOWN"


# --- fills, partial cancels, P&L consistency ---------------------------------------------------------


async def test_duplicate_fill_delivery_is_applied_once(tmp_path):
    with harness(tmp_path) as h:
        await h.oms.start()
        h.quote(1000.0)
        await h.oms.submit(intent(Side.BUY, 100))
        (f,) = h.quote(1000.0)
        h.oms._on_fill(f)  # the broker re-delivers the same fill
        assert h.book.quantity(INFY.key, Product.CNC) == 100
        assert len(h.events("FillReceived")) == 1


async def test_a_partial_cancel_reports_the_true_filled_quantity(tmp_path):
    with harness(tmp_path, cash="5000000", config=FillModelConfig(max_fill_quotes=3)) as h:
        await h.oms.start()
        h.quote(1000.0, volume=0)
        result = await h.oms.submit(intent(Side.BUY, 1000))
        for _ in range(4):
            h.quote(1000.0, volume=1000)  # 100 shares available per quote
        order = h.oms.orders[result.order.client_order_id]  # type: ignore[union-attr]
        assert order.status is OrderStatus.CANCELLED and order.filled_qty == 300
        (cancelled,) = h.events("OrderCancelled")
        assert cancelled.filled_qty == 300
        assert h.book.quantity(INFY.key, Product.CNC) == 300


async def test_trade_pnl_projection_equals_engine_and_broker_pnl(tmp_path):
    """The audit F-01 shape at the OMS: buy 100 @1000, sell 50 @1021, sell 50 @1041 -> flat, and
    the trades projection (what a dashboard reads) = the book's P&L = the broker's P&L."""
    with harness(tmp_path, costs=NSECostSchedule.from_yaml(), market=MarketContext()) as h:
        await h.oms.start()
        h.quote(1000.0)
        await h.oms.submit(intent(Side.BUY, 100))
        h.quote(1000.0)
        await h.oms.submit(intent(Side.SELL, 50, leg="partial"))
        h.quote(1021.0)
        await h.oms.submit(intent(Side.SELL, 50, leg="final"))
        h.quote(1041.0)
        assert h.book.quantity(INFY.key, Product.CNC) == 0
        trades = h.store.query("SELECT net_pnl FROM trades")
        dashboard = sum((Decimal(r["net_pnl"]) for r in trades), Decimal(0))
        (broker_pos,) = await h.broker.get_positions()
        assert broker_pos.quantity == 0
        assert dashboard == h.book.realized_pnl == broker_pos.realized_pnl
        assert h.book.cash - h.book.starting_cash == h.book.realized_pnl  # flat: P&L is all cash


# --- reconciliation -------------------------------------------------------------------------------------


async def test_reconcile_in_sync_after_normal_trading(tmp_path):
    with harness(tmp_path, costs=NSECostSchedule.from_yaml()) as h:
        await h.oms.start()
        h.quote(1000.0)
        await h.oms.submit(intent(Side.BUY, 100))
        h.quote(1001.0)
        result = await h.oms.reconcile()
        assert result.in_sync and result.diffs == ()


async def test_reconcile_adopts_missed_fills_and_flags_drift(tmp_path):
    with harness(tmp_path) as h:
        h.quote(1000.0)
        await h.oms.submit(intent(Side.BUY, 100))  # not subscribed: the fill is "missed"
        h.quote(1001.0)
        assert h.book.quantity(INFY.key, Product.CNC) == 0
        result = await h.oms.reconcile()
        assert h.book.quantity(INFY.key, Product.CNC) == 100  # adopted from the broker
        assert any(d.startswith("adopted missed fill") for d in result.diffs)
        assert result.in_sync  # after adoption nothing else differs

        h.book.cash -= Decimal(1)  # now corrupt the local book
        drift = await h.oms.reconcile()
        assert not drift.in_sync and any("cash" in d for d in drift.diffs)
        assert [a.key for a in h.events("Alert")] == ["SYS_RECON_DRIFT"]
