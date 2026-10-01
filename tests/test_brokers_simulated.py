"""Plan M3.3: the SimulatedBroker fill model, rejections, state and determinism."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.brokers.base import InvalidRequestError, OrderRejectedError, RejectReason
from src.brokers.simulated.broker import SimulatedBroker
from src.brokers.simulated.costs import NSECostSchedule
from src.brokers.simulated.fill_model import (
    FillModelConfig,
    MarketContext,
    adverse_price,
    half_spread_bps,
    round_to_tick,
)
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.ids import client_order_id
from src.domain.types import (
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    MarketDataSource,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Product,
    Quote,
    Side,
)
from src.store.event_store import EventStore
from src.store.kv import KVStateStore
from src.utils.market_time import IST

DAY = date(2026, 10, 5)
INFY = Instrument.nse_equity("INFY", tick_size=Decimal("0.05"))
BANDED = Instrument.nse_equity("SMALLCO", band_pct=5.0)
LIQUID = MarketContext(adv_inr=1e10, adv_shares=1e7, sigma_daily=0.02)  # 1 bp half-spread


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), IST)


def make_broker(
    *,
    clock: ReplayClock | None = None,
    cash: str = "1000000",
    costs: NSECostSchedule | None = None,
    **kw: Any,
) -> tuple[SimulatedBroker, ReplayClock]:
    clock = clock or ReplayClock(at(9, 30))
    broker = SimulatedBroker(
        book_id="A",
        instruments={INFY.key: INFY, BANDED.key: BANDED},
        clock=clock,
        calendar=get_calendar(),
        costs=costs or NSECostSchedule.zero(),
        starting_cash=Decimal(cash),
        market={INFY.key: LIQUID, BANDED.key: LIQUID},
        **kw,
    )
    return broker, clock


def quote(
    ltp: float,
    volume: int,
    received: datetime,
    instrument: Instrument = INFY,
    prev_close: float | None = 1000.0,
) -> Quote:
    return Quote(
        instrument_key=instrument.key,
        ltp=ltp,
        prev_close=prev_close,
        volume_cum=volume,
        exchange_ts=received - timedelta(minutes=15),
        receipt_ts=received,
        source=MarketDataSource.YFINANCE,
        is_delayed=True,
    )


def order(
    side: Side,
    qty: int,
    *,
    leg: str = "entry",
    order_type: OrderType = OrderType.MARKET,
    trigger: str | None = None,
    instrument: Instrument = INFY,
    product: Product = Product.CNC,
) -> Order:
    opening = side is Side.BUY
    intent_id = f"A:momentum:{instrument.key}:2026-10-02:{leg}"
    intent = OrderIntent(
        intent_id=intent_id,
        decision_id="d1",
        book_id="A",
        strategy="momentum",
        instrument=instrument,
        side=side,
        kind=IntentKind.OPEN if opening else IntentKind.CLOSE,
        quantity=qty,
        order_type=order_type,
        product=product,
        trigger_price=Decimal(trigger) if trigger else None,
        reduce_only=not opening,
        decision_price=Decimal("1000"),
        decision_ts=at(9, 20),
        reason=IntentReason.ENTRY if opening else IntentReason.STOP,
        source=IntentSource.SIGNAL_ENGINE if opening else IntentSource.EXIT_MANAGER,
    )
    return Order(client_order_id=client_order_id(intent_id), intent=intent, quantity=qty)


async def buy_and_fill(broker: SimulatedBroker, clock: ReplayClock, qty: int = 100) -> None:
    broker.on_quote(quote(1000.0, 10_000, clock.now()))
    await broker.place_order(order(Side.BUY, qty))
    later = clock.now() + timedelta(minutes=1)
    fills = broker.on_quote(quote(1000.0, 10_000 + qty * 20, later))
    assert sum(f.quantity for f in fills) == qty


# --- fill model -------------------------------------------------------------------------------


def test_spread_tiers_and_adverse_tick_rounding():
    cfg = FillModelConfig()
    assert half_spread_bps(cfg, 6e9) == 1.0 and half_spread_bps(cfg, 6e8) == 3.0
    assert half_spread_bps(cfg, 6e7) == 8.0 and half_spread_bps(cfg, 1e6) == 20.0
    assert half_spread_bps(cfg, None) == 20.0
    tick = Decimal("0.05")
    assert round_to_tick(Decimal("100.01"), tick, Side.BUY) == Decimal("100.05")
    assert round_to_tick(Decimal("100.04"), tick, Side.SELL) == Decimal("100.00")
    # Even a zero-cost model moves at least half a tick, so the fill is a full tick away.
    zero = MarketContext(adv_inr=1e12, adv_shares=1e12)
    price = adverse_price(
        Decimal("100.00"), Side.BUY, tick=tick, config=cfg, context=zero, quantity=1
    )
    assert price == Decimal("100.05")


# --- market orders -----------------------------------------------------------------------------


async def test_market_order_fills_on_the_first_quote_after_latency():
    broker, clock = make_broker()
    broker.on_quote(quote(1000.0, 10_000, at(9, 30)))
    ack = await broker.place_order(order(Side.BUY, 10))
    assert ack.status is OrderStatus.OPEN
    assert broker.on_quote(quote(1001.0, 10_100, at(9, 30))) == []  # same instant: latency
    (fill,) = broker.on_quote(quote(1002.0, 20_000, at(9, 31)))
    assert fill.quantity == 10 and fill.price == Decimal("1002.15")  # +1 bp, rounded up
    assert fill.fill_id == f"{ack.broker_order_id}-F1" and fill.ts == at(9, 31)
    funds = await broker.get_funds()
    assert funds.available_cash == Decimal("1000000") - Decimal("10021.50")
    (holding,) = await broker.get_holdings()
    assert holding.quantity == 10 and holding.avg_price == Decimal("1002.15")
    snap = await broker.find_order_by_tag(ack.client_order_id)
    assert snap is not None and snap.status is OrderStatus.FILLED


async def test_partial_fill_then_cancel_reports_the_true_filled_quantity():
    broker, clock = make_broker(cash="2000000")
    broker.on_quote(quote(1000.0, 0, at(9, 30)))
    ack = await broker.place_order(order(Side.BUY, 1000))
    volume = 0
    for minute in range(1, 7):  # 1,000 shares trade per minute -> 100 per quote at 10%
        volume += 1000
        broker.on_quote(quote(1000.0, volume, at(9, 30 + minute)))
    snap = await broker.get_order(ack.broker_order_id)
    assert snap.status is OrderStatus.CANCELLED
    assert snap.filled_qty == 500  # five eligible quotes x 100 shares, then the rest cancelled
    assert "remainder 500 cancelled" in snap.message
    assert sum(f.quantity for f in await broker.get_trades()) == 500


async def test_participation_is_shared_by_orders_on_the_same_quote():
    broker, _ = make_broker()
    broker.on_quote(quote(1000.0, 0, at(9, 30)))
    await broker.place_order(order(Side.BUY, 80, leg="a"))
    await broker.place_order(order(Side.BUY, 80, leg="b"))
    fills = broker.on_quote(quote(1000.0, 1000, at(9, 31)))  # 100 shares available in total
    assert sum(f.quantity for f in fills) == 100


# --- rejections ----------------------------------------------------------------------------------


async def test_cnc_short_is_rejected():
    broker, clock = make_broker()
    broker.on_quote(quote(1000.0, 10_000, clock.now()))
    with pytest.raises(OrderRejectedError) as exc:
        await broker.place_order(order(Side.SELL, 10))
    assert exc.value.reason is RejectReason.SHORT_NOT_ALLOWED


async def test_cnc_sell_cannot_exceed_holdings_less_pending_sells():
    broker, clock = make_broker()
    await buy_and_fill(broker, clock, 100)
    await broker.place_order(
        order(Side.SELL, 60, leg="stop", order_type=OrderType.SL_M, trigger="950.00")
    )
    with pytest.raises(OrderRejectedError) as exc:
        await broker.place_order(order(Side.SELL, 50, leg="target"))
    assert exc.value.reason is RejectReason.SHORT_NOT_ALLOWED
    await broker.place_order(order(Side.SELL, 40, leg="target2"))  # 100 - 60 pending = 40 ok


@pytest.mark.parametrize(
    ("when", "reason"),
    [
        (at(8, 0), RejectReason.MARKET_CLOSED),
        (at(10, 0, day=date(2026, 10, 2)), RejectReason.MARKET_CLOSED),
    ],
    ids=["before-open", "holiday"],
)
async def test_market_closed_is_rejected(when, reason):
    broker, _ = make_broker(clock=ReplayClock(when))
    broker.on_quote(quote(1000.0, 10_000, when))
    with pytest.raises(OrderRejectedError) as exc:
        await broker.place_order(order(Side.BUY, 10))
    assert exc.value.reason is reason


async def test_insufficient_funds_is_rejected():
    broker, clock = make_broker(cash="5000")
    broker.on_quote(quote(1000.0, 10_000, clock.now()))
    with pytest.raises(OrderRejectedError) as exc:
        await broker.place_order(order(Side.BUY, 10))
    assert exc.value.reason is RejectReason.INSUFFICIENT_FUNDS


async def test_band_tick_lot_and_order_type_checks():
    broker, clock = make_broker()
    broker.on_quote(quote(105.5, 10_000, clock.now(), instrument=BANDED, prev_close=100.0))
    with pytest.raises(OrderRejectedError) as exc:
        await broker.place_order(order(Side.BUY, 10, instrument=BANDED))
    assert exc.value.reason is RejectReason.PRICE_BAND
    await buy_and_fill(broker, clock, 100)
    with pytest.raises(OrderRejectedError) as exc:
        await broker.place_order(
            order(Side.SELL, 10, leg="s", order_type=OrderType.SL_M, trigger="950.03")
        )
    assert exc.value.reason is RejectReason.TICK_SIZE
    with pytest.raises(InvalidRequestError, match="not supported"):
        await broker.place_order(order(Side.BUY, 10, leg="mis", product=Product.NRML))


async def test_the_same_tag_is_never_placed_twice():
    broker, clock = make_broker()
    broker.on_quote(quote(1000.0, 10_000, clock.now()))
    first = await broker.place_order(order(Side.BUY, 10))
    again = await broker.place_order(order(Side.BUY, 10))
    assert again.broker_order_id == first.broker_order_id
    assert len(await broker.get_orders()) == 1


# --- stops -------------------------------------------------------------------------------------------


async def test_stop_rests_then_fills_at_the_worse_of_trigger_and_price():
    broker, clock = make_broker()
    await buy_and_fill(broker, clock, 100)
    ack = await broker.place_order(
        order(Side.SELL, 100, leg="stop", order_type=OrderType.SL_M, trigger="950.00")
    )
    assert ack.status is OrderStatus.TRIGGER_PENDING
    assert broker.on_quote(quote(960.0, 30_000, at(9, 40))) == []
    (fill,) = broker.on_quote(quote(930.0, 40_000, at(9, 41)))  # gapped through 950
    assert fill.price == Decimal("929.85")  # 930 - 1 bp, rounded down: the gap price, not 950


async def test_a_gap_through_a_stop_fills_at_the_open():
    clock = ReplayClock(at(9, 30))
    broker, _ = make_broker(clock=clock)
    await buy_and_fill(broker, clock, 100)
    broker.expire_session(at(15, 30))  # DAY stops expire; the exit manager re-places them

    day2 = date(2026, 10, 6)
    await clock.advance_to(at(9, 15, 5, day=day2))
    broker.on_quote(quote(1010.0, 50_000, at(9, 15, 1, day=day2), prev_close=1010.0))
    await broker.place_order(
        order(Side.SELL, 100, leg="stop-2026-10-06", order_type=OrderType.SL_M, trigger="980.00")
    )
    # The day's first fresh quote opens far below the stop (an overnight gap).
    (fill,) = broker.on_quote(quote(900.0, 60_000, at(9, 30, day=day2), prev_close=1010.0))
    # The open (900) moved by spread + impact, nowhere near the 980 trigger.
    expected = adverse_price(
        Decimal("900"),
        Side.SELL,
        tick=INFY.tick_size,
        config=FillModelConfig(),
        context=LIQUID,
        quantity=100,
    )
    assert fill.price == expected == Decimal("899.85")
    assert (await broker.get_holdings()) == []


async def test_stops_are_not_cancelled_for_lack_of_liquidity():
    broker, clock = make_broker(config=FillModelConfig(max_fill_quotes=2))
    await buy_and_fill(broker, clock, 100)
    ack = await broker.place_order(
        order(Side.SELL, 100, leg="stop", order_type=OrderType.SL_M, trigger="950.00")
    )
    volume = 12_000  # cumulative volume after buy_and_fill
    for minute in range(40, 46):  # each quote only has 10 shares of liquidity
        volume += 100
        broker.on_quote(quote(940.0, volume, at(9, minute)))
    snap = await broker.get_order(ack.broker_order_id)
    assert snap.status is OrderStatus.PARTIALLY_FILLED and snap.filled_qty == 60


# --- day end, settlement, charges --------------------------------------------------------------------


async def test_unfilled_day_orders_expire_and_release_reserved_cash():
    broker, clock = make_broker()
    broker.on_quote(quote(1000.0, 0, clock.now()))
    ack = await broker.place_order(order(Side.BUY, 100))
    assert (await broker.get_funds()).blocked > 0
    (expired,) = broker.expire_session(at(15, 30))
    assert expired.status is OrderStatus.EXPIRED
    funds = await broker.get_funds()
    assert funds.blocked == 0 and funds.available_cash == Decimal("1000000")
    assert (await broker.get_order(ack.broker_order_id)).status is OrderStatus.EXPIRED


async def test_t_plus_1_sale_proceeds():
    clock = ReplayClock(at(9, 30))
    broker, _ = make_broker(clock=clock, sell_proceeds="t_plus_1")
    await buy_and_fill(broker, clock, 100)
    cash_after_buy = (await broker.get_funds()).available_cash
    await broker.place_order(order(Side.SELL, 100, leg="exit"))
    broker.on_quote(quote(1000.0, 50_000, at(9, 45)))
    funds = await broker.get_funds()
    assert funds.available_cash == cash_after_buy and funds.unsettled_credit > 0
    await clock.advance_to(at(9, 16, day=date(2026, 10, 6)))
    funds = await broker.get_funds()
    assert funds.unsettled_credit == 0 and funds.available_cash > cash_after_buy


async def test_dp_is_charged_once_per_isin_per_day():
    broker, clock = make_broker(costs=NSECostSchedule.from_yaml())
    await buy_and_fill(broker, clock, 100)
    await broker.place_order(order(Side.SELL, 50, leg="exit-1"))
    await broker.place_order(order(Side.SELL, 50, leg="exit-2"))
    fills = broker.on_quote(quote(1000.0, 100_000, at(9, 50)))
    dp = [f.charges_breakdown.get("dp", Decimal(0)) for f in fills]
    assert sorted(dp) == [Decimal(0), Decimal("12.50")]


# --- subscriptions, persistence, determinism -----------------------------------------------------------


async def test_order_updates_and_fills_are_pushed_to_subscribers():
    broker, clock = make_broker()
    updates, fills = [], []
    sub = await broker.subscribe_order_updates(updates.append, fills.append)
    await buy_and_fill(broker, clock, 10)
    assert [u.status for u in updates] == [OrderStatus.OPEN, OrderStatus.FILLED]
    assert len(fills) == 1
    await sub.close()
    await broker.place_order(order(Side.SELL, 10, leg="x"))
    assert len(updates) == 2


async def test_state_survives_a_restart_through_the_store(tmp_path):
    with EventStore(tmp_path / "rq.db") as store:
        clock = ReplayClock(at(9, 30))
        broker, _ = make_broker(clock=clock, state_store=KVStateStore(store, "A", "sim_broker"))
        await buy_and_fill(broker, clock, 100)
        reborn, _ = make_broker(
            clock=clock, state_store=KVStateStore(store, "A", "sim_broker"), cash="1"
        )
        assert (await reborn.get_funds()) == (await broker.get_funds())
        assert (await reborn.get_positions()) == (await broker.get_positions())
        assert (await reborn.get_orders()) == (await broker.get_orders())
        assert store.snapshot_projections()["orders"] == []  # broker state is not an event


async def _scenario() -> list[tuple[str, int, Decimal, datetime]]:
    broker, clock = make_broker(costs=NSECostSchedule.from_yaml())
    broker.on_quote(quote(1000.0, 0, at(9, 30)))
    await broker.place_order(order(Side.BUY, 300, leg="a"))
    await broker.place_order(order(Side.BUY, 150, leg="b"))
    volume = 0
    for n, px in enumerate([1001.0, 999.5, 1003.2, 1002.0, 998.7]):
        volume += 1500
        broker.on_quote(quote(px, volume, at(9, 30) + timedelta(seconds=20 * (n + 1))))
    return [(f.fill_id, f.quantity, f.price, f.ts) for f in await broker.get_trades()]


async def test_golden_fills_are_deterministic():
    first = await _scenario()
    assert first == await _scenario()
    # Each quote offers 10% of 1,500 = 150 shares, taken FIFO by order; prices are the quote
    # plus spread and impact, rounded up to the tick.
    golden = [
        ("SIM-A-000001-F1", 150, Decimal("1001.20"), at(9, 30, 20)),
        ("SIM-A-000001-F2", 150, Decimal("999.70"), at(9, 30, 40)),
        ("SIM-A-000002-F1", 150, Decimal("1003.40"), at(9, 31, 0)),
    ]
    assert first == golden
