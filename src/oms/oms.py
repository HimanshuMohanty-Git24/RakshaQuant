"""
The OMS (plan M3.4; audit §H.5): the **single** entry point for every order - entries, exits,
flattens and operator orders - and the only writer of the book.

``submit(intent)``:

1. Reduce-only intents are clipped to what the position still has to give (net of other live
   reduce orders), so an exit can never open or flip a position. Nothing left -> BLOCKED.
2. The pre-trade ``gate`` decides - the :class:`~src.risk.gate.RiskGate` in production; it may
   resize or reject, and an OMS without a gate routes nothing.
3. ``client_order_id = sha256(intent_id)[:16]``: a repeated intent returns the existing order.
4. ``OrderSubmitted`` is persisted **before** the broker call, and the write is **never retried**.
   A ``NOT_PLACED`` error -> REJECTED. An ``UNKNOWN`` outcome (timeout, 5xx, anything unexpected)
   -> UNKNOWN, resolved later by :meth:`resolve_unknown` via ``find_order_by_tag``.

Fills arrive from the broker's update stream; each is applied once (by ``fill_id``) to the
:class:`PositionBook` and recorded as ``FillReceived`` + ``PositionChanged`` (+ ``TradeClosed`` for
anything it closed). Status updates move forward only, along the §H.5 state machine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from src.brokers.base import (
    BrokerAdapter,
    BrokerError,
    BrokerOrderSnapshot,
    OrderRejectedError,
    Subscription,
)
from src.domain.base import EventPayload
from src.domain.clock import Clock
from src.domain.events import (
    Alert,
    FillReceived,
    OrderAcked,
    OrderCancelled,
    OrderExpired,
    OrderRejected,
    OrderSubmitted,
    OrderUnknown,
    PositionChanged,
    ReconciliationResult,
    TradeClosed,
)
from src.domain.ids import client_order_id
from src.domain.sink import EventSink
from src.domain.types import Fill, Order, OrderIntent, OrderStatus, Product, Side
from src.oms.position_book import PositionBook

logger = logging.getLogger(__name__)

S = OrderStatus
# Allowed status moves (audit §H.5). Fills may skip ahead (a fast fill straight from SUBMITTED).
_NEXT: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.PENDING_NEW: frozenset({S.SUBMITTED, S.REJECTED}),
    S.SUBMITTED: frozenset(
        {
            S.OPEN,
            S.TRIGGER_PENDING,
            S.REJECTED,
            S.UNKNOWN,
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.CANCELLED,
            S.EXPIRED,
        }
    ),  # fmt: skip
    S.UNKNOWN: frozenset(
        {
            S.OPEN,
            S.TRIGGER_PENDING,
            S.REJECTED,
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.CANCELLED,
            S.EXPIRED,
        }
    ),  # fmt: skip
    S.TRIGGER_PENDING: frozenset(
        {S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.PENDING_CANCEL, S.CANCELLED, S.EXPIRED}
    ),
    S.OPEN: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.PENDING_CANCEL, S.CANCELLED, S.EXPIRED}),
    S.PARTIALLY_FILLED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.PENDING_CANCEL, S.CANCELLED, S.EXPIRED}
    ),
    S.PENDING_CANCEL: frozenset({S.CANCELLED, S.PARTIALLY_FILLED, S.FILLED}),
    S.PENDING_MODIFY: frozenset({S.OPEN, S.TRIGGER_PENDING, S.PARTIALLY_FILLED, S.FILLED}),
}

SubmitStatus = Literal["SUBMITTED", "DUPLICATE", "BLOCKED", "REJECTED", "UNKNOWN"]


@dataclass(frozen=True)
class GateResult:
    """The pre-trade decision: ``quantity`` 0 means rejected."""

    quantity: int
    reason: str = ""


class OrderGate(Protocol):
    async def __call__(self, intent: OrderIntent, quantity: int | None) -> GateResult: ...


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    order: Order | None = None
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in ("SUBMITTED", "DUPLICATE", "UNKNOWN")


FillListener = Callable[[Fill, Order], None]
EventListener = Callable[[EventPayload], None]


class OMS:
    def __init__(
        self,
        *,
        book_id: str,
        broker: BrokerAdapter,
        book: PositionBook,
        sink: EventSink,
        clock: Clock,
        gate: OrderGate | None = None,
        unknown_timeout_s: float = 30.0,
    ) -> None:
        if book.book_id != book_id:
            raise ValueError("the OMS and its PositionBook must share the book id")
        self.book_id = book_id
        self.broker = broker
        self.book = book
        self._sink = sink
        self._clock = clock
        self._gate = gate
        self._unknown_timeout_s = unknown_timeout_s
        self.orders: dict[str, Order] = {}  # by client_order_id
        self._unknown_checks: dict[str, int] = {}
        self._fill_listeners: list[FillListener] = []
        self._event_listeners: list[EventListener] = []
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        """Subscribe to the broker's order updates and fills."""
        if self._subscription is None:
            self._subscription = await self.broker.subscribe_order_updates(
                self._on_update, self._on_fill
            )

    async def stop(self) -> None:
        if self._subscription is not None:
            await self._subscription.close()
            self._subscription = None

    def use_gate(self, gate: OrderGate) -> None:
        """Install the pre-trade gate (the RiskGate is built after the OMS it reads from)."""
        self._gate = gate

    def add_fill_listener(self, listener: FillListener) -> None:
        """Called after each new fill is applied to the book (e.g. the exit manager)."""
        self._fill_listeners.append(listener)

    def add_event_listener(self, listener: EventListener) -> None:
        """Called with every event the OMS records, after it is recorded (e.g. the risk state)."""
        self._event_listeners.append(listener)

    # -- submission --------------------------------------------------------------------------------

    async def submit(self, intent: OrderIntent, quantity: int | None = None) -> SubmitResult:
        if intent.book_id != self.book_id:
            return SubmitResult("BLOCKED", message=f"intent is for book {intent.book_id}")
        if self._gate is None:  # nothing reaches a broker without a pre-trade decision
            return SubmitResult("BLOCKED", message="no risk gate installed: refusing to route")
        coid = client_order_id(intent.intent_id)
        existing = self.orders.get(coid)
        if existing is not None:
            return SubmitResult("DUPLICATE", existing, "intent already submitted")

        qty = quantity if quantity is not None else intent.quantity
        if intent.reduce_only:
            available = self.reducible(intent)
            qty = available if qty is None else min(qty, available)
            if qty <= 0:
                return SubmitResult("BLOCKED", message="reduce-only: nothing left to reduce")
        decision = await self._gate(intent, qty)
        if decision.quantity <= 0:
            return SubmitResult("BLOCKED", message=decision.reason or "rejected by the gate")
        qty = decision.quantity if qty is None else min(qty, decision.quantity)
        if qty is None or qty <= 0:
            return SubmitResult("BLOCKED", message="intent has no quantity (unsized)")

        now = self._clock.now()
        order = Order(
            client_order_id=coid,
            intent=intent,
            quantity=qty,
            status=OrderStatus.SUBMITTED,
            submitted_at=now,
        )
        self.orders[coid] = order
        self._emit(OrderSubmitted(order=order), source="oms")  # before the broker call
        try:
            ack = await self.broker.place_order(order)
        except BrokerError as exc:
            if exc.outcome == "UNKNOWN":
                return self._mark_unknown(order, f"{type(exc).__name__}: {exc}")
            reason = exc.reason.value if isinstance(exc, OrderRejectedError) else type(exc).__name__
            self._set(order, status=OrderStatus.REJECTED)
            self._emit(
                OrderRejected(**self._ref(order), reason=reason, message=str(exc)), source="oms"
            )
            return SubmitResult("REJECTED", self.orders[coid], f"{reason}: {exc}")
        except Exception as exc:  # the write may or may not have happened: never assume
            logger.exception("unexpected error placing %s", coid)
            return self._mark_unknown(order, f"{type(exc).__name__}: {exc}")

        current = self.orders[coid]
        if current.status is OrderStatus.SUBMITTED:  # no update callback beat the response
            self._set(current, broker_order_id=ack.broker_order_id)
            self._advance(current, ack.status)
            self._emit(
                OrderAcked(
                    **self._ref(current),
                    broker_order_id=ack.broker_order_id,
                    status=self.orders[coid].status,
                ),
                source="oms",
            )
        return SubmitResult("SUBMITTED", self.orders[coid])

    def reducible(self, intent: OrderIntent) -> int:
        """How much a reduce-only intent may still take off (never enough to flip)."""
        position = self.book.quantity(intent.instrument.key, intent.product)
        if position == 0 or (position > 0) == (intent.side is Side.BUY):
            return 0
        committed = sum(
            o.remaining_qty
            for o in self.orders.values()
            if o.intent.reduce_only
            and o.intent.instrument.key == intent.instrument.key
            and o.intent.product is intent.product
            and not o.status.is_terminal
        )
        return max(0, abs(position) - committed)

    def _mark_unknown(self, order: Order, error: str) -> SubmitResult:
        self._set(order, status=OrderStatus.UNKNOWN)
        self._unknown_checks[order.client_order_id] = 0
        self._emit(OrderUnknown(**self._ref(order), error=error), source="oms")
        logger.warning("order %s outcome UNKNOWN: %s", order.client_order_id, error)
        return SubmitResult("UNKNOWN", self.orders[order.client_order_id], error)

    async def cancel(self, client_order_id_: str) -> None:
        order = self.orders.get(client_order_id_)
        if order is None or order.status.is_terminal or order.broker_order_id is None:
            return
        await self.broker.cancel_order(order.broker_order_id)

    # -- broker callbacks ------------------------------------------------------------------------------

    def _on_update(self, snap: BrokerOrderSnapshot) -> None:
        order = self.orders.get(snap.client_order_id)
        if order is None:
            self._alert(
                "oms_unknown_order", f"broker update for unknown order {snap.client_order_id}"
            )
            return
        if order.broker_order_id is None:
            self._set(order, broker_order_id=snap.broker_order_id)
        if snap.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return  # driven by fills
        if not self._advance(order, snap.status):
            return
        order = self.orders[snap.client_order_id]
        ref = self._ref(order)
        if snap.status is OrderStatus.CANCELLED:
            self._emit(
                OrderCancelled(**ref, filled_qty=snap.filled_qty, reason=snap.message), source="oms"
            )
        elif snap.status is OrderStatus.EXPIRED:
            self._emit(OrderExpired(**ref, filled_qty=snap.filled_qty), source="oms")
        elif snap.status is OrderStatus.REJECTED:
            self._emit(
                OrderRejected(**ref, reason="BROKER_REJECTED", message=snap.message), source="oms"
            )
        else:  # acknowledged (it may arrive before place_order returns), or a stop triggering
            self._emit(
                OrderAcked(**ref, broker_order_id=snap.broker_order_id, status=snap.status),
                source="oms",
            )

    def _on_fill(self, fill: Fill) -> None:
        if self.book.has_seen(fill.fill_id):
            return
        order = self.orders.get(fill.client_order_id)
        if order is None:
            self._alert("oms_unknown_fill", f"fill {fill.fill_id} for unknown order; applied")
            # The broker is the source of truth: book it (as CNC, the month-1 product) and alert.
            outcome = self.book.apply(fill, product=Product.CNC, strategy="unknown")
            self._emit(
                PositionChanged(position=outcome.position, fill_id=fill.fill_id), source="oms"
            )
            return
        filled = order.filled_qty + fill.quantity
        if filled > order.quantity:
            self._alert("oms_overfill", f"{fill.fill_id} overfills {order.client_order_id}")
            filled = order.quantity
        prior = (order.avg_fill_price or Decimal(0)) * order.filled_qty
        avg = (prior + fill.price * fill.quantity) / (order.filled_qty + fill.quantity)
        status = OrderStatus.FILLED if filled == order.quantity else OrderStatus.PARTIALLY_FILLED
        self._set(
            order,
            filled_qty=filled,
            avg_fill_price=avg,
            broker_order_id=fill.broker_order_id or order.broker_order_id,
        )
        self._advance(self.orders[order.client_order_id], status, force=True)
        order = self.orders[order.client_order_id]
        intent = order.intent
        outcome = self.book.apply(
            fill, product=intent.product, strategy=intent.strategy, exit_reason=intent.reason.value
        )
        self._emit(
            FillReceived(
                fill=fill, order_status=order.status, order_filled_qty=filled, order_avg_price=avg
            ),
            source="oms",
        )
        self._emit(PositionChanged(position=outcome.position, fill_id=fill.fill_id), source="oms")
        for piece in outcome.closed:
            self._emit(
                TradeClosed(
                    trade_id=piece.trade_id,
                    book_id=self.book_id,
                    decision_id=piece.decision_id,
                    exit_decision_id=piece.exit_decision_id,
                    instrument_key=piece.instrument_key,
                    strategy=piece.strategy,
                    side=piece.side,
                    quantity=piece.quantity,
                    entry_price=piece.entry_price,
                    exit_price=piece.exit_price,
                    entry_ts=piece.entry_ts,
                    exit_ts=piece.exit_ts,
                    gross_pnl=piece.gross_pnl,
                    charges=piece.charges,
                    net_pnl=piece.net_pnl,
                    exit_reason=piece.exit_reason or "unknown",
                ),
                source="oms",
            )
        for listener in list(self._fill_listeners):
            listener(fill, order)

    # -- UNKNOWN resolution and reconciliation ----------------------------------------------------------

    async def resolve_unknown(self) -> None:
        """Look UNKNOWN orders up by tag; reject those still absent after two checks + timeout."""
        now = self._clock.now()
        for coid in [c for c, o in self.orders.items() if o.status is OrderStatus.UNKNOWN]:
            order = self.orders[coid]
            snap = await self.broker.find_order_by_tag(coid)
            if snap is not None:
                self._set(order, broker_order_id=snap.broker_order_id)
                await self._adopt_fills(since=order.submitted_at)
                if not self.orders[coid].status.is_terminal and snap.status not in (
                    OrderStatus.FILLED,
                    OrderStatus.PARTIALLY_FILLED,
                ):
                    self._advance(self.orders[coid], snap.status)
                self._emit(
                    OrderAcked(**self._ref(order), broker_order_id=snap.broker_order_id,
                               status=self.orders[coid].status),
                    source="oms",
                )  # fmt: skip
                self._unknown_checks.pop(coid, None)
                continue
            self._unknown_checks[coid] = self._unknown_checks.get(coid, 0) + 1
            waited = (now - (order.submitted_at or now)).total_seconds()
            if self._unknown_checks[coid] >= 2 and waited >= self._unknown_timeout_s:
                self._set(order, status=OrderStatus.REJECTED)
                self._emit(
                    OrderRejected(**self._ref(order), reason="NOT_FOUND_AT_BROKER",
                                  message=f"absent after {self._unknown_checks[coid]} checks"),
                    source="oms",
                )  # fmt: skip
                self._unknown_checks.pop(coid, None)

    async def _adopt_fills(self, since: datetime | None = None) -> list[str]:
        missing = [
            f for f in await self.broker.get_trades(since) if not self.book.has_seen(f.fill_id)
        ]
        for fill in missing:
            self._on_fill(fill)
        return [f.fill_id for f in missing]

    async def reconcile(self) -> ReconciliationResult:
        """Compare with the broker (the source of truth): adopt missed fills, report drift."""
        diffs: list[str] = []
        adopted = await self._adopt_fills()
        diffs += [f"adopted missed fill {fid}" for fid in adopted]

        broker_orders = {s.client_order_id: s for s in await self.broker.get_orders()}
        for coid, order in self.orders.items():
            snap = broker_orders.get(coid)
            if snap is None:
                if order.status not in (OrderStatus.REJECTED, OrderStatus.UNKNOWN):
                    diffs.append(f"order {coid} ({order.status}) unknown to the broker")
            elif snap.status is not order.status:
                diffs.append(f"order {coid}: OMS {order.status} vs broker {snap.status}")
        for coid in broker_orders.keys() - self.orders.keys():
            diffs.append(f"broker order {coid} unknown to the OMS")

        now = self._clock.now()
        ours = {(p.instrument_key, p.product): p.quantity for p in self.book.positions(now)}
        theirs = {
            (p.instrument_key, p.product): p.quantity for p in await self.broker.get_positions()
        }
        for key in sorted(ours.keys() | theirs.keys()):
            if ours.get(key, 0) != theirs.get(key, 0):
                diffs.append(
                    f"position {key[0]}/{key[1]}: OMS {ours.get(key, 0)} vs broker {theirs.get(key, 0)}"
                )

        funds = await self.broker.get_funds()
        broker_cash = funds.available_cash + funds.blocked + funds.unsettled_credit
        if broker_cash != self.book.cash:
            diffs.append(f"cash: OMS {self.book.cash} vs broker {broker_cash}")

        in_sync = not [d for d in diffs if not d.startswith("adopted")]
        result = ReconciliationResult(
            book_id=self.book_id, scope="oms_broker", in_sync=in_sync, diffs=tuple(diffs)
        )
        self._emit(result, source="oms")
        if not in_sync:
            self._alert("SYS_RECON_DRIFT", "; ".join(diffs), level="CRITICAL")
        return result

    # -- helpers ------------------------------------------------------------------------------------------

    def _advance(self, order: Order, status: OrderStatus, *, force: bool = False) -> bool:
        """Move an order forward along the state machine; ignore stale or illegal moves."""
        current = order.status
        if status is current and status is not OrderStatus.PARTIALLY_FILLED:
            return False
        if not force and status not in _NEXT.get(current, frozenset()):
            if not current.is_terminal:
                logger.warning("ignoring %s -> %s for %s", current, status, order.client_order_id)
            return False
        self._set(order, status=status)
        return True

    def _set(self, order: Order, **changes: object) -> None:
        latest = self.orders.get(order.client_order_id, order)
        self.orders[order.client_order_id] = latest.model_copy(
            update={**changes, "version": latest.version + 1}
        )

    def _ref(self, order: Order) -> dict[str, str]:
        return {
            "client_order_id": order.client_order_id,
            "book_id": order.intent.book_id,
            "decision_id": order.intent.decision_id,
            "instrument_key": order.intent.instrument.key,
        }

    def _emit(self, payload: EventPayload, *, source: str = "oms") -> None:
        self._sink.emit(payload, source=source)
        for listener in list(self._event_listeners):
            try:
                listener(payload)
            except Exception:  # a listener never breaks order handling
                logger.exception("event listener failed on %s", payload.event_type)
                self._sink.emit(
                    Alert(level="CRITICAL", key="oms_listener_failed",
                          message=f"listener failed on {payload.event_type}"),
                    source=source,
                )  # fmt: skip

    def _alert(self, key: str, message: str, *, level: str = "WARNING") -> None:
        logger.warning("%s: %s", key, message)
        self._emit(Alert(level=level, key=key, message=message), source="oms")
