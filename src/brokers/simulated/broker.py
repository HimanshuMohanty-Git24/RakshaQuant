"""
SimulatedBroker (plan M3.3; audit §M): an NSE-like exchange for paper trading that implements
:class:`~src.brokers.base.BrokerAdapter`, so the OMS runs exactly the code path it would run
against a real broker.

Month-1 scope:

* **MARKET** orders fill on quotes received after ``submit + latency``, at the reference price
  moved by half-spread + impact and rounded adversely to the tick (see ``fill_model``).
* **SL-M** orders rest until the price crosses the trigger, then fill on that same quote at the
  worse of the trigger and the price (so a gap through a stop fills at the gap price).
* **Participation cap:** each quote fills at most ``participation`` x the volume traded since the
  previous quote. A MARKET order still incomplete after ``max_fill_quotes`` eligible quotes is
  cancelled for the remainder; the cancellation reports the true ``filled_qty``.
* **Rejections:** market closed, unknown instrument, off-lot quantity, off-tick trigger, price
  beyond the scrip's band, CNC sell beyond holdings (no delivery shorts), insufficient funds.
* **Cash:** CNC buys need 100% cash (reserved on acceptance). Sale proceeds are available the
  same day or on the next trading day (``sell_proceeds``).
* **State** (cash, holdings, orders, fills) is the broker's own, persisted through a
  :class:`StateStore` after every change; the OMS reconciles against it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from src.brokers.base import (
    BrokerAck,
    BrokerAdapter,
    BrokerCapabilities,
    BrokerOrderSnapshot,
    FillCallback,
    Funds,
    Holding,
    InvalidRequestError,
    MarginQuote,
    OrderRejectedError,
    OrderUpdateCallback,
    QuoteCallback,
    RejectReason,
)
from src.brokers.simulated.costs import NSECostSchedule
from src.brokers.simulated.fill_model import (
    FillModelConfig,
    MarketContext,
    adverse_price,
    on_tick,
    rng_for,
    sample_latency_ms,
)
from src.domain.calendar import CalendarCoverageError, NSECalendar
from src.domain.clock import Clock
from src.domain.types import (
    Fill,
    Instrument,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    Product,
    Quote,
    Side,
    Validity,
)
from src.store.event_store import EventStore
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

SellProceeds = Literal["same_day", "t_plus_1"]
_FUNDS_BUFFER = Decimal("0.005")  # reserve 0.5% over the reference price for buys
_LIVE = (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.TRIGGER_PENDING)

SIM_CAPABILITIES = BrokerCapabilities(
    order_types=frozenset({OrderType.MARKET, OrderType.SL_M}),
    products=frozenset({Product.CNC, Product.MIS}),
    validities=frozenset({Validity.DAY}),
    supports_modify=True,
    tag_max_len=20,
    lookup_by_tag=True,
    order_stream=True,
    mis_square_off=time(15, 15),
)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class StateStore(Protocol):
    def load(self) -> str | None: ...

    def save(self, value: str, ts: datetime) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self.value: str | None = None

    def load(self) -> str | None:
        return self.value

    def save(self, value: str, ts: datetime) -> None:
        self.value = value


class KVStateStore:
    """Keeps the broker's book in the event store's key/value table."""

    def __init__(self, store: EventStore, key: str, namespace: str = "sim_broker") -> None:
        self._store, self._namespace, self._key = store, namespace, key

    def load(self) -> str | None:
        return self._store.kv_get(self._namespace, self._key)

    def save(self, value: str, ts: datetime) -> None:
        self._store.kv_put(self._namespace, self._key, value, ts)


class _SimOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_order_id: str
    client_order_id: str
    decision_id: str
    instrument_key: str
    side: Side
    product: Product
    order_type: OrderType
    quantity: int
    trigger_price: Decimal | None = None
    status: OrderStatus
    filled_qty: int = 0
    filled_notional: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)
    eligible_at: datetime
    eligible_quotes: int = 0
    fill_count: int = 0
    session_day: date
    created_at: datetime
    updated_at: datetime
    message: str = ""

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_qty

    def snapshot(self) -> BrokerOrderSnapshot:
        avg = (self.filled_notional / self.filled_qty) if self.filled_qty else None
        return BrokerOrderSnapshot(
            broker_order_id=self.broker_order_id,
            client_order_id=self.client_order_id,
            instrument_key=self.instrument_key,
            side=self.side,
            product=self.product,
            order_type=self.order_type,
            quantity=self.quantity,
            filled_qty=self.filled_qty,
            avg_fill_price=avg,
            trigger_price=self.trigger_price,
            status=self.status,
            updated_at=self.updated_at,
            message=self.message,
        )


class _SimPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int = 0  # signed
    cost: Decimal = Decimal(0)  # |quantity| x average price
    realized: Decimal = Decimal(0)  # net of charges
    updated_at: datetime | None = None


class _Unsettled(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_on: date
    amount: Decimal


class _SimState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cash: Decimal
    blocked: Decimal = Decimal(0)
    unsettled: list[_Unsettled] = []
    positions: dict[str, _SimPosition] = {}  # "<instrument_key>|<product>"
    orders: dict[str, _SimOrder] = {}  # by broker_order_id
    fills: list[Fill] = []
    next_order: int = 1
    dp_days: list[str] = []  # "<instrument_key>|<date>": DP already charged that day
    volume: dict[str, tuple[date, int]] = {}  # last seen cumulative volume per instrument


class _Subscription:
    def __init__(self, on_close: Callable[[], None]) -> None:
        self._on_close = on_close

    async def close(self) -> None:
        self._on_close()


def _pos_key(instrument_key: str, product: Product) -> str:
    return f"{instrument_key}|{product.value}"


# ---------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------


class SimulatedBroker:
    def __init__(
        self,
        *,
        book_id: str,
        instruments: Mapping[str, Instrument],
        clock: Clock,
        calendar: NSECalendar,
        costs: NSECostSchedule,
        starting_cash: Decimal,
        state_store: StateStore | None = None,
        config: FillModelConfig | None = None,
        market: Mapping[str, MarketContext] | None = None,
        sell_proceeds: SellProceeds = "same_day",
        capabilities: BrokerCapabilities = SIM_CAPABILITIES,
    ) -> None:
        self.name = f"sim:{book_id}"
        self.book_id = book_id
        self._instruments = dict(instruments)
        self._clock = clock
        self._calendar = calendar
        self._costs = costs
        self._config = config or FillModelConfig()
        self._market = dict(market or {})
        self._sell_proceeds = sell_proceeds
        self._caps = capabilities
        self._store = state_store or MemoryStateStore()
        saved = self._store.load()
        self._state = (
            _SimState.model_validate_json(saved) if saved else _SimState(cash=starting_cash)
        )
        self._dirty = False
        self._quotes: dict[str, Quote] = {}
        self._quote_subscribers: list[QuoteCallback] = []
        self._update_subscribers: list[tuple[OrderUpdateCallback, FillCallback]] = []

    # -- configuration -----------------------------------------------------------------------

    def set_market_context(self, market: Mapping[str, MarketContext]) -> None:
        self._market = dict(market)

    def capabilities(self) -> BrokerCapabilities:
        return self._caps

    async def authenticate(self) -> None:
        return None

    async def ensure_session(self) -> None:
        return None

    async def load_instruments(self) -> list[Instrument]:
        return list(self._instruments.values())

    # -- market data ---------------------------------------------------------------------------

    async def subscribe_market_data(
        self,
        instruments: Sequence[Instrument],
        on_quote: QuoteCallback,
        mode: Literal["ltp", "quote", "depth"] = "ltp",
    ) -> _Subscription:
        self._quote_subscribers.append(on_quote)
        return _Subscription(lambda: self._quote_subscribers.remove(on_quote))

    async def get_quote(self, instruments: Sequence[Instrument]) -> dict[str, Quote]:
        return {i.key: self._quotes[i.key] for i in instruments if i.key in self._quotes}

    def on_quote(self, quote: Quote) -> list[Fill]:
        """Feed one market quote: settles cash, expires old orders, and matches orders."""
        today = quote.receipt_ts.astimezone(IST).date()
        self._settle(today)
        interval = self._interval_volume(quote, today)
        self._quotes[quote.instrument_key] = quote
        for callback in list(self._quote_subscribers):
            callback(quote)

        fills: list[Fill] = []
        liquidity = int(self._config.participation * interval)
        live = sorted(
            (o for o in self._state.orders.values() if o.instrument_key == quote.instrument_key),
            key=lambda o: o.broker_order_id,
        )
        for order in live:
            if order.status not in _LIVE:
                continue
            if order.session_day < today:
                self._finish(order, OrderStatus.EXPIRED, "day order expired", quote.receipt_ts)
                continue
            filled, used = self._match(order, quote, liquidity)
            liquidity -= used
            fills.extend(filled)
        if self._dirty:  # persist only when orders or cash changed, not on every quote
            self._save()
        return fills

    def _interval_volume(self, quote: Quote, today: date) -> int:
        previous = self._state.volume.get(quote.instrument_key)
        self._state.volume[quote.instrument_key] = (today, quote.volume_cum)
        if previous is not None and previous[0] == today:
            return max(0, quote.volume_cum - previous[1])
        return quote.volume_cum

    # -- matching ------------------------------------------------------------------------------

    def _match(self, order: _SimOrder, quote: Quote, liquidity: int) -> tuple[list[Fill], int]:
        if quote.receipt_ts < order.eligible_at:  # not at the exchange yet (latency)
            return [], 0
        ltp = Decimal(str(quote.ltp))
        reference = ltp
        if order.status is OrderStatus.TRIGGER_PENDING:
            trigger = order.trigger_price
            if trigger is None:  # pragma: no cover - SL-M intents always carry a trigger
                self._finish(order, OrderStatus.REJECTED, "SL-M without trigger", quote.receipt_ts)
                return [], 0
            crossed = ltp <= trigger if order.side is Side.SELL else ltp >= trigger
            if not crossed:
                return [], 0
            # A resting stop fills on the triggering quote at the worse of trigger and price,
            # so a gap through the stop fills at the gap price.
            reference = min(trigger, ltp) if order.side is Side.SELL else max(trigger, ltp)
            order.status = OrderStatus.OPEN
            order.updated_at = quote.receipt_ts
            self._dirty = True
            self._notify_update(order)

        order.eligible_quotes += 1
        instrument = self._instruments[order.instrument_key]
        lot = instrument.lot_size
        qty = min(order.remaining, max(0, liquidity))
        qty -= qty % lot
        fills: list[Fill] = []
        if qty > 0:
            fill = self._execute(order, instrument, reference, qty, quote)
            if fill is not None:
                fills.append(fill)
        # A MARKET order that keeps finding too little liquidity is cancelled for the remainder.
        # Stops are never cancelled for liquidity: they keep working until filled or the day ends.
        if (
            order.order_type is OrderType.MARKET
            and order.remaining > 0
            and order.status in _LIVE
            and order.eligible_quotes >= self._config.max_fill_quotes
        ):
            self._finish(
                order,
                OrderStatus.CANCELLED,
                f"unfilled remainder {order.remaining} cancelled after "
                f"{order.eligible_quotes} quotes (participation cap)",
                quote.receipt_ts,
            )
        return fills, sum(f.quantity for f in fills)

    def _execute(
        self, order: _SimOrder, instrument: Instrument, reference: Decimal, qty: int, quote: Quote
    ) -> Fill | None:
        context = self._market.get(order.instrument_key, MarketContext())
        price = adverse_price(
            reference,
            order.side,
            tick=instrument.tick_size,
            config=self._config,
            context=context,
            quantity=qty,
        )
        notional = price * qty
        day = quote.receipt_ts.astimezone(IST).date()
        dp_key = f"{order.instrument_key}|{day.isoformat()}"
        dp_applies = (
            order.side is Side.SELL
            and order.product is Product.CNC
            and dp_key not in self._state.dp_days
        )
        charges = self._costs.charges(
            product=order.product, side=order.side, notional=notional, dp_applies=dp_applies
        )
        if order.side is Side.BUY:
            cost = notional + charges.total
            release = min(order.reserved, cost)
            if cost > release + self._available():
                self._finish(
                    order,
                    OrderStatus.CANCELLED,
                    f"remainder cancelled: insufficient funds for {qty} @ {price}",
                    quote.receipt_ts,
                )
                return None
            order.reserved -= release
            self._state.blocked -= release
            self._state.cash -= cost
        else:
            proceeds = notional - charges.total
            if self._sell_proceeds == "same_day":
                self._state.cash += proceeds
            else:
                self._state.unsettled.append(
                    _Unsettled(available_on=self._calendar.next_trading_day(day), amount=proceeds)
                )
        if dp_applies:
            self._state.dp_days.append(dp_key)

        self._apply_position(order, qty, price, charges.total, quote.receipt_ts)
        self._dirty = True
        order.fill_count += 1
        order.filled_qty += qty
        order.filled_notional += notional
        order.updated_at = quote.receipt_ts
        fill = Fill(
            fill_id=f"{order.broker_order_id}-F{order.fill_count}",
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            book_id=self.book_id,
            decision_id=order.decision_id,
            instrument_key=order.instrument_key,
            side=order.side,
            quantity=qty,
            price=price,
            ts=quote.receipt_ts,
            charges=charges.total,
            charges_breakdown={k: v for k, v in charges.as_dict().items() if v > 0},
        )
        self._state.fills.append(fill)
        if order.remaining == 0:
            self._finish(order, OrderStatus.FILLED, "", quote.receipt_ts, notify=False)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        for _, on_fill in list(self._update_subscribers):
            on_fill(fill)
        self._notify_update(order)
        return fill

    def _apply_position(
        self, order: _SimOrder, qty: int, price: Decimal, charges: Decimal, ts: datetime
    ) -> None:
        pos = self._state.positions.setdefault(
            _pos_key(order.instrument_key, order.product), _SimPosition()
        )
        signed = qty if order.side is Side.BUY else -qty
        pos.realized -= charges
        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):  # open or add
            pos.cost += price * qty
            pos.quantity += signed
        else:  # reduce, close, or (MIS only) flip
            direction = 1 if pos.quantity > 0 else -1
            closing = min(qty, abs(pos.quantity))
            avg = pos.cost / abs(pos.quantity)
            pos.realized += (price - avg) * closing * direction
            pos.cost -= avg * closing
            pos.quantity -= direction * closing
            rest = qty - closing
            if rest:  # CNC never gets here: sells beyond holdings are rejected
                pos.quantity = -direction * rest
                pos.cost = price * rest
        if pos.quantity == 0:
            pos.cost = Decimal(0)
        pos.updated_at = ts

    # -- orders ---------------------------------------------------------------------------------

    async def place_order(self, order: Order) -> BrokerAck:
        now = self._clock.now()
        existing = self._find(order.client_order_id)
        if existing is not None:  # same tag: never place twice
            return self._ack(existing, "duplicate tag: existing order returned")
        intent = order.intent
        self._validate(order, intent, now)
        instrument = self._instruments[intent.instrument.key]
        reference = Decimal(str(self._quotes[instrument.key].ltp))

        reserved = Decimal(0)
        if intent.side is Side.BUY:
            reserved = self._buy_reservation(intent.product, reference, order.quantity)
            if reserved > self._available():
                raise OrderRejectedError(
                    f"needs {reserved} but {self._available()} available",
                    reason=RejectReason.INSUFFICIENT_FUNDS,
                )
        day = now.astimezone(IST).date()
        latency = timedelta(
            milliseconds=sample_latency_ms(rng_for(order.client_order_id, day), self._config)
        )
        sim = _SimOrder(
            broker_order_id=f"SIM-{self.book_id}-{self._state.next_order:06d}",
            client_order_id=order.client_order_id,
            decision_id=intent.decision_id,
            instrument_key=instrument.key,
            side=intent.side,
            product=intent.product,
            order_type=intent.order_type,
            quantity=order.quantity,
            trigger_price=intent.trigger_price,
            status=(
                OrderStatus.TRIGGER_PENDING
                if intent.order_type is OrderType.SL_M
                else OrderStatus.OPEN
            ),
            reserved=reserved,
            eligible_at=now + latency,
            session_day=day,
            created_at=now,
            updated_at=now,
        )
        self._state.next_order += 1
        self._state.blocked += reserved
        self._state.orders[sim.broker_order_id] = sim
        self._save()
        self._notify_update(sim)
        return self._ack(sim)

    def _validate(self, order: Order, intent: OrderIntent, now: datetime) -> None:
        if order.quantity <= 0:
            raise InvalidRequestError("quantity must be positive")
        if not self._caps.supports(intent.order_type, intent.product, intent.validity):
            raise InvalidRequestError(
                f"{intent.order_type}/{intent.product}/{intent.validity} not supported"
            )
        instrument = self._instruments.get(intent.instrument.key)
        if instrument is None:
            raise OrderRejectedError(
                f"unknown instrument {intent.instrument.key}",
                reason=RejectReason.INSTRUMENT_RESTRICTED,
            )
        if order.quantity % instrument.lot_size:
            raise InvalidRequestError(f"quantity must be a multiple of {instrument.lot_size}")
        try:
            open_now = self._calendar.is_market_open(now)
        except CalendarCoverageError:
            open_now = False
        if not open_now:
            raise OrderRejectedError("market closed", reason=RejectReason.MARKET_CLOSED)
        if intent.product is Product.MIS and not instrument.mis_allowed:
            raise OrderRejectedError(
                f"MIS not allowed for {instrument.key}", reason=RejectReason.PRODUCT_NOT_ALLOWED
            )
        quote = self._quotes.get(instrument.key)
        if quote is None:
            raise OrderRejectedError(
                f"no market price for {instrument.key}", reason=RejectReason.RMS_OTHER
            )
        if intent.trigger_price is not None and not on_tick(
            intent.trigger_price, instrument.tick_size
        ):
            raise OrderRejectedError(
                f"trigger {intent.trigger_price} is off the {instrument.tick_size} tick",
                reason=RejectReason.TICK_SIZE,
            )
        if instrument.band_pct is not None and quote.prev_close:
            for label, price in (("price", quote.ltp), ("trigger", intent.trigger_price)):
                if price is None:
                    continue
                move = abs(float(price) / quote.prev_close - 1) * 100
                if move >= instrument.band_pct:
                    raise OrderRejectedError(
                        f"{label} {price} is at/through the {instrument.band_pct}% band",
                        reason=RejectReason.PRICE_BAND,
                    )
        if intent.product is Product.CNC and intent.side is Side.SELL:
            held = self._state.positions.get(_pos_key(instrument.key, Product.CNC))
            pending = sum(
                o.remaining
                for o in self._state.orders.values()
                if o.instrument_key == instrument.key
                and o.product is Product.CNC
                and o.side is Side.SELL
                and o.status in _LIVE
            )
            available = (held.quantity if held and held.quantity > 0 else 0) - pending
            if order.quantity > available:
                raise OrderRejectedError(
                    f"CNC sell of {order.quantity} exceeds {available} available to sell",
                    reason=RejectReason.SHORT_NOT_ALLOWED,
                )

    def _buy_reservation(self, product: Product, reference: Decimal, qty: int) -> Decimal:
        notional = reference * qty * (1 + _FUNDS_BUFFER)
        charges = self._costs.charges(product=product, side=Side.BUY, notional=notional)
        return (notional + charges.total).quantize(Decimal("0.01"))

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        qty: int | None = None,
        price: Decimal | None = None,
        trigger: Decimal | None = None,
    ) -> BrokerAck:
        order = self._get(broker_order_id)
        if order.status not in _LIVE:
            raise InvalidRequestError(f"{broker_order_id} is {order.status}; cannot modify")
        if price is not None:
            raise InvalidRequestError("limit prices are not supported")
        instrument = self._instruments[order.instrument_key]
        if trigger is not None:
            if order.order_type is not OrderType.SL_M:
                raise InvalidRequestError("only SL-M orders have a trigger")
            if not on_tick(trigger, instrument.tick_size):
                raise OrderRejectedError(
                    f"trigger {trigger} is off the tick", reason=RejectReason.TICK_SIZE
                )
            order.trigger_price = trigger
        if qty is not None:
            if qty < order.filled_qty or qty <= 0 or qty % instrument.lot_size:
                raise InvalidRequestError(f"invalid quantity {qty}")
            order.quantity = qty
        order.updated_at = self._clock.now()
        if order.remaining == 0:
            self._finish(order, OrderStatus.FILLED, "", order.updated_at)
        self._save()
        self._notify_update(order)
        return self._ack(order, "modified")

    async def cancel_order(self, broker_order_id: str) -> BrokerAck:
        order = self._get(broker_order_id)
        if order.status in _LIVE:
            self._finish(order, OrderStatus.CANCELLED, "cancelled by client", self._clock.now())
            self._save()
        return self._ack(order)

    def expire_session(self, now: datetime) -> list[BrokerOrderSnapshot]:
        """End of day: every DAY order still live expires (call at the session close)."""
        expired = []
        for order in self._state.orders.values():
            if order.status in _LIVE:
                self._finish(order, OrderStatus.EXPIRED, "day order expired", now)
                expired.append(order.snapshot())
        if expired:
            self._save()
        return expired

    def _finish(
        self,
        order: _SimOrder,
        status: OrderStatus,
        message: str,
        ts: datetime,
        *,
        notify: bool = True,
    ) -> None:
        order.status = status
        order.message = message
        order.updated_at = ts
        self._dirty = True
        if order.reserved:
            self._state.blocked -= order.reserved
            order.reserved = Decimal(0)
        if notify:
            self._notify_update(order)

    # -- queries ---------------------------------------------------------------------------------

    async def get_order(self, broker_order_id: str) -> BrokerOrderSnapshot:
        return self._get(broker_order_id).snapshot()

    async def find_order_by_tag(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        order = self._find(client_order_id)
        return None if order is None else order.snapshot()

    async def get_orders(self) -> list[BrokerOrderSnapshot]:
        return [o.snapshot() for o in self._state.orders.values()]

    async def get_trades(self, since: datetime | None = None) -> list[Fill]:
        return [f for f in self._state.fills if since is None or f.ts >= since]

    async def subscribe_order_updates(
        self, on_update: OrderUpdateCallback, on_fill: FillCallback
    ) -> _Subscription:
        pair = (on_update, on_fill)
        self._update_subscribers.append(pair)
        return _Subscription(lambda: self._update_subscribers.remove(pair))

    async def get_positions(self) -> list[Position]:
        out = []
        for key, pos in self._state.positions.items():
            instrument_key, product = key.rsplit("|", 1)
            out.append(
                Position(
                    book_id=self.book_id,
                    instrument_key=instrument_key,
                    product=Product(product),
                    quantity=pos.quantity,
                    avg_price=(pos.cost / abs(pos.quantity)) if pos.quantity else None,
                    realized_pnl=pos.realized,
                    updated_at=pos.updated_at or self._clock.now(),
                )
            )
        return out

    async def get_holdings(self) -> list[Holding]:
        return [
            Holding(
                instrument_key=key.rsplit("|", 1)[0],
                quantity=pos.quantity,
                avg_price=pos.cost / pos.quantity,
            )
            for key, pos in self._state.positions.items()
            if key.endswith(f"|{Product.CNC.value}") and pos.quantity > 0
        ]

    async def get_funds(self) -> Funds:
        self._settle(self._clock.now().astimezone(IST).date())
        return Funds(
            available_cash=max(Decimal(0), self._available()),
            blocked=self._state.blocked,
            unsettled_credit=sum((u.amount for u in self._state.unsettled), Decimal(0)),
        )

    async def get_margin_required(self, intents: Sequence[OrderIntent]) -> MarginQuote:
        required = Decimal(0)
        for intent in intents:
            if intent.side is not Side.BUY or intent.quantity is None:
                continue
            quote = self._quotes.get(intent.instrument.key)
            reference = Decimal(str(quote.ltp)) if quote else intent.decision_price
            required += self._buy_reservation(intent.product, reference, intent.quantity)
        return MarginQuote(required=required, available=max(Decimal(0), self._available()))

    @property
    def cash(self) -> Decimal:
        """Settled cash including amounts reserved for open buy orders."""
        return self._state.cash

    # -- internals ----------------------------------------------------------------------------------

    def _available(self) -> Decimal:
        return self._state.cash - self._state.blocked

    def _settle(self, today: date) -> None:
        due = [u for u in self._state.unsettled if u.available_on <= today]
        if due:
            self._state.cash += sum((u.amount for u in due), Decimal(0))
            self._state.unsettled = [u for u in self._state.unsettled if u.available_on > today]

    def _find(self, client_order_id: str) -> _SimOrder | None:
        for order in self._state.orders.values():
            if order.client_order_id == client_order_id:
                return order
        return None

    def _get(self, broker_order_id: str) -> _SimOrder:
        try:
            return self._state.orders[broker_order_id]
        except KeyError:
            raise InvalidRequestError(f"unknown order {broker_order_id}") from None

    def _ack(self, order: _SimOrder, message: str = "") -> BrokerAck:
        return BrokerAck(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=order.status,
            ts=order.updated_at,
            message=message or order.message,
        )

    def _notify_update(self, order: _SimOrder) -> None:
        snapshot = order.snapshot()
        for on_update, _ in list(self._update_subscribers):
            on_update(snapshot)

    def _save(self) -> None:
        self._store.save(self._state.model_dump_json(), self._clock.now())
        self._dirty = False


def _conforms_to_protocol(broker: SimulatedBroker) -> BrokerAdapter:
    """Static check (mypy): SimulatedBroker implements the BrokerAdapter protocol."""
    return broker
