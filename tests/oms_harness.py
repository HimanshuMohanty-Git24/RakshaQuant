"""A wired OMS + SimulatedBroker + PositionBook on a real event store, for OMS/exit tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.brokers.simulated.broker import SimulatedBroker
from src.brokers.simulated.costs import NSECostSchedule
from src.brokers.simulated.fill_model import FillModelConfig, MarketContext
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.types import (
    Fill,
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    MarketDataSource,
    OrderIntent,
    OrderType,
    Product,
    Quote,
    Side,
)
from src.oms.oms import OMS
from src.oms.position_book import PositionBook
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.utils.market_time import IST

DAY = date(2026, 10, 5)
INFY = Instrument.nse_equity("INFY", tick_size=Decimal("0.05"))
DEEP = MarketContext(adv_inr=1e13, adv_shares=1e12, sigma_daily=0.0)  # ~zero spread/impact


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), IST)


@dataclass
class Harness:
    clock: ReplayClock
    store: EventStore
    broker: SimulatedBroker
    book: PositionBook
    oms: OMS
    volume: int = 0
    last_received: datetime | None = None

    def quote(self, ltp: float, *, minutes: float = 1, volume: int = 1_000_000) -> list[Fill]:
        """Advance the received time by ``minutes`` and feed a quote with ample liquidity."""
        self.volume += volume
        base = max(self.clock.now(), self.last_received or self.clock.now())
        received = base + timedelta(minutes=minutes)
        self.last_received = received
        return self.broker.on_quote(
            Quote(
                instrument_key=INFY.key,
                ltp=ltp,
                prev_close=1000.0,
                volume_cum=self.volume,
                exchange_ts=received - timedelta(minutes=15),
                receipt_ts=received,
                source=MarketDataSource.YFINANCE,
                is_delayed=True,
            )
        )

    async def at_minute(self, delta_minutes: float) -> None:
        await self.clock.advance(delta_minutes * 60)

    def events(self, *types: str) -> list[Any]:
        return [e.payload for e in self.store.read(types=list(types))]


@contextmanager
def harness(
    tmp: Path,
    *,
    cash: str = "1000000",
    costs: NSECostSchedule | None = None,
    market: MarketContext = DEEP,
    config: FillModelConfig | None = None,
    start: datetime | None = None,
) -> Iterator[Harness]:
    clock = ReplayClock(start or at(9, 30))
    with EventStore(tmp / "rq.db") as store:
        broker = SimulatedBroker(
            book_id="A",
            instruments={INFY.key: INFY},
            clock=clock,
            calendar=get_calendar(),
            costs=costs or NSECostSchedule.zero(),
            starting_cash=Decimal(cash),
            market={INFY.key: market},
            config=config,
        )
        book = PositionBook("A", Decimal(cash))
        oms = OMS(
            book_id="A", broker=broker, book=book, sink=StoreSink(store, clock, "oms"), clock=clock
        )
        yield Harness(clock, store, broker, book, oms)


def intent(
    side: Side,
    qty: int | None,
    *,
    leg: str = "entry",
    kind: IntentKind | None = None,
    order_type: OrderType = OrderType.MARKET,
    trigger: str | None = None,
    reason: IntentReason | None = None,
    decision_id: str = "d1",
    bar: str = "2026-10-02",
) -> OrderIntent:
    kind = kind or (IntentKind.OPEN if side is Side.BUY else IntentKind.CLOSE)
    reducing = kind.reduces_risk
    return OrderIntent(
        intent_id=f"A:momentum:{INFY.key}:{bar}:{leg}",
        decision_id=decision_id,
        book_id="A",
        strategy="momentum",
        instrument=INFY,
        side=side,
        kind=kind,
        quantity=qty,
        order_type=order_type,
        product=Product.CNC,
        trigger_price=Decimal(trigger) if trigger else None,
        reduce_only=reducing,
        decision_price=Decimal("1000"),
        decision_ts=at(9, 20),
        reason=reason or (IntentReason.ENTRY if not reducing else IntentReason.TARGET),
        source=IntentSource.SIGNAL_ENGINE if not reducing else IntentSource.EXIT_MANAGER,
    )
