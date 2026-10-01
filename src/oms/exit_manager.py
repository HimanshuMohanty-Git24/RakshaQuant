"""
Exit management for CNC swing positions (plan M3.5; ported from ``src/execution/exit_manager.py``
with the audit F-01/F-06/F-09/F-19 fixes).

* Every exit is a **reduce-only** intent through ``OMS.submit`` (so it can never oversell or
  open a short), with ``parent_decision_id`` = the entry's decision.
* Stop and target are anchored to the **fill** price: ``stop = fill - k_stop x ATR``,
  ``target = fill + k_target x ATR``.
* The stop is a **broker-side SL-M** order. It is a DAY order, so it is re-placed every session
  (and whenever the managed quantity or the trailing level changes) under a new, versioned leg.
* Exit fills **decrement** the managed quantity; whatever remains is re-covered by a stop.
* CNC exits: target, optional partial at ``partial_at_r`` x R, ``max_hold_days`` sessions, and a
  trailing stop on the daily ATR that only ever ratchets up. **No** minute-based stale/time exits
  and **no** regime-change exits.
* Restart: state is persisted; :meth:`reconcile` adopts book positions it does not manage (when an
  ATR is known) or alerts, and drops managed entries the book no longer holds.

Legs are deterministic (``stop-<day>-v<n>``, ``target``, ``partial``, ``time-<day>``), so a restart
re-submitting an action hits the OMS's duplicate check instead of placing twice.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from src.brokers.simulated.fill_model import round_to_tick
from src.domain.calendar import NSECalendar
from src.domain.clock import Clock
from src.domain.events import Alert
from src.domain.ids import client_order_id, intent_id, new_id
from src.domain.sink import EventSink
from src.domain.types import (
    Fill,
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    Order,
    OrderIntent,
    OrderType,
    Product,
    Quote,
    Side,
)
from src.oms.oms import OMS
from src.store.kv import MemoryStateStore, StateStore
from src.utils.market_time import IST

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExitPolicy:
    k_stop_atr: float = 2.0
    k_target_atr: float = 3.0
    k_trail_atr: float | None = 2.0  # None disables trailing
    max_hold_days: int = 10  # sessions
    partial_at_r: float | None = None  # e.g. 1.0 = take a partial at 1R; None disables
    partial_fraction: float = 0.5

    def __post_init__(self) -> None:
        if self.k_stop_atr <= 0 or self.k_target_atr <= 0:
            raise ValueError("stop and target multiples must be positive")
        if self.max_hold_days < 1:
            raise ValueError("max_hold_days must be >= 1")
        if not 0 < self.partial_fraction < 1:
            raise ValueError("partial_fraction must be in (0, 1)")


class ManagedPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument: Instrument
    product: Product
    strategy: str
    entry_decision_id: str
    signal_bar_date: date
    entry_price: Decimal
    quantity: int
    stop_price: Decimal
    target_price: Decimal
    r_distance: Decimal
    atr: Decimal
    entered_on: date
    highest_close: Decimal
    partial_taken: bool = False
    stop_coid: str | None = None
    stop_version: int = 0
    closing: bool = False  # a full exit has been submitted

    @property
    def key(self) -> str:
        return f"{self.instrument.key}|{self.product.value}"


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: dict[str, ManagedPosition] = {}
    pending_atr: dict[str, tuple[Decimal, str, str, date]] = {}  # entry coid -> (atr, ...)


class ExitManager:
    def __init__(
        self,
        *,
        oms: OMS,
        clock: Clock,
        calendar: NSECalendar,
        sink: EventSink,
        policy: ExitPolicy | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._oms = oms
        self._clock = clock
        self._calendar = calendar
        self._sink = sink
        self.policy = policy or ExitPolicy()
        self._store = state_store or MemoryStateStore()
        saved = self._store.load()
        self._state = _State.model_validate_json(saved) if saved else _State()
        self._stale_stops: set[str] = set()  # positions whose stop must be (re)placed
        oms.add_fill_listener(self._on_fill)

    # -- wiring ------------------------------------------------------------------------------

    def expect_entry(self, entry: OrderIntent, *, atr: Decimal, signal_bar_date: date) -> None:
        """Tell the manager the ATR behind an entry so it can set levels from the fill."""
        if atr <= 0:
            raise ValueError("ATR must be positive")
        self._state.pending_atr[client_order_id(entry.intent_id)] = (
            atr,
            entry.strategy,
            entry.decision_id,
            signal_bar_date,
        )
        self._save()

    @property
    def positions(self) -> dict[str, ManagedPosition]:
        return dict(self._state.positions)

    # -- fills (sync callback from the OMS) ------------------------------------------------------

    def _on_fill(self, fill: Fill, order: Order) -> None:
        intent = order.intent
        key = f"{intent.instrument.key}|{intent.product.value}"
        held = self._oms.book.quantity(intent.instrument.key, intent.product)
        if not intent.reduce_only:
            self._on_entry_fill(fill, order, key, held)
        else:
            pos = self._state.positions.get(key)
            if pos is None:
                return
            if held <= 0:
                del self._state.positions[key]
                logger.info("exit complete for %s", key)
            else:
                pos.quantity = held
                if intent.reason is IntentReason.PARTIAL:
                    pos.partial_taken = True
                self._stale_stops.add(key)  # re-cover the remainder
        self._save()

    def _on_entry_fill(self, fill: Fill, order: Order, key: str, held: int) -> None:
        pending = self._state.pending_atr.get(order.client_order_id)
        pos = self._state.positions.get(key)
        if pending is not None:
            atr, strategy, decision_id, bar = pending
        elif pos is not None:  # adding to a managed position: keep its ATR and lineage
            atr, strategy, decision_id, bar = (
                pos.atr,
                pos.strategy,
                pos.entry_decision_id,
                pos.signal_bar_date,
            )
        else:
            self._alert(
                "exit_unmanaged_entry", f"entry fill {fill.fill_id} without an ATR: no stop"
            )
            return
        entry = self._oms.book.position(order.intent.instrument.key, order.intent.product, fill.ts)
        entry_price = entry.avg_price or fill.price
        stop, target = self._levels(entry_price, atr, order.intent.instrument)
        day = fill.ts.astimezone(IST).date()
        self._state.positions[key] = ManagedPosition(
            instrument=order.intent.instrument,
            product=order.intent.product,
            strategy=strategy,
            entry_decision_id=decision_id,
            signal_bar_date=bar,
            entry_price=entry_price,
            quantity=held,
            stop_price=stop,
            target_price=target,
            r_distance=entry_price - stop,
            atr=atr,
            entered_on=pos.entered_on if pos else day,
            highest_close=entry_price,
            partial_taken=pos.partial_taken if pos else False,
            stop_coid=pos.stop_coid if pos else None,
            stop_version=pos.stop_version if pos else 0,
        )
        if order.status.is_terminal:
            self._state.pending_atr.pop(order.client_order_id, None)
        self._stale_stops.add(key)

    def _levels(
        self, entry: Decimal, atr: Decimal, instrument: Instrument
    ) -> tuple[Decimal, Decimal]:
        tick = instrument.tick_size
        stop = round_to_tick(entry - Decimal(str(self.policy.k_stop_atr)) * atr, tick, Side.SELL)
        target = round_to_tick(entry + Decimal(str(self.policy.k_target_atr)) * atr, tick, Side.BUY)
        return max(stop, tick), target

    # -- the monitor's entry points ---------------------------------------------------------------

    async def on_quote(self, quote: Quote) -> None:
        """Place pending stops, then check target / partial for this instrument."""
        await self.refresh_stops()
        price = Decimal(str(quote.ltp))
        for key, pos in list(self._state.positions.items()):
            if pos.instrument.key != quote.instrument_key or pos.closing:
                continue
            if price >= pos.target_price:
                await self._exit_all(pos, IntentReason.TARGET, "target", price)
            elif (
                self.policy.partial_at_r is not None
                and not pos.partial_taken
                and price
                >= pos.entry_price + Decimal(str(self.policy.partial_at_r)) * pos.r_distance
            ):
                await self._exit_partial(pos, price)
        self._save()

    async def on_session_start(
        self, day: date, *, atr: Mapping[str, Decimal], closes: Mapping[str, Decimal]
    ) -> None:
        """Daily: time exits, trailing-stop update, and re-placing the (expired) DAY stops."""
        for key, pos in list(self._state.positions.items()):
            if pos.closing:
                continue
            held_sessions = len(self._calendar.trading_days(pos.entered_on, day)) - 1
            if held_sessions >= self.policy.max_hold_days:
                await self._exit_all(pos, IntentReason.TIME, f"time-{day.isoformat()}", None)
                continue
            close = closes.get(pos.instrument.key)
            if close is not None:
                pos.highest_close = max(pos.highest_close, close)
            new_atr = atr.get(pos.instrument.key, pos.atr)
            if self.policy.k_trail_atr is not None:
                trail = round_to_tick(
                    pos.highest_close - Decimal(str(self.policy.k_trail_atr)) * new_atr,
                    pos.instrument.tick_size,
                    Side.SELL,
                )
                if trail > pos.stop_price:
                    pos.stop_price = trail
            pos.stop_coid = None  # yesterday's DAY stop has expired
            self._stale_stops.add(key)
        await self.refresh_stops()
        self._save()

    async def refresh_stops(self) -> None:
        """(Re)place the stop for every position whose stop is missing or out of date."""
        for key in sorted(self._stale_stops):
            pos = self._state.positions.get(key)
            if pos is None or pos.closing or pos.quantity <= 0:
                self._stale_stops.discard(key)
                continue
            if pos.stop_coid is None:
                live = self._live_stop(pos)
                if (
                    live is not None
                    and live.remaining_qty == pos.quantity
                    and live.intent.trigger_price
                ):
                    # e.g. after a restart: the stop is still resting at the broker; adopt it.
                    pos.stop_coid = live.client_order_id
                    pos.stop_price = live.intent.trigger_price
                    self._stale_stops.discard(key)
                    continue
                if live is not None:
                    await self._oms.cancel(live.client_order_id)
            else:
                current = self._oms.orders.get(pos.stop_coid)
                if current is not None and not current.status.is_terminal:
                    await self._oms.cancel(pos.stop_coid)
            pos.stop_version += 1
            day = self._clock.now().astimezone(IST).date()
            result = await self._oms.submit(
                self._intent(
                    pos,
                    f"stop-{day.isoformat()}-v{pos.stop_version}",
                    IntentKind.CLOSE,
                    IntentReason.STOP,
                    pos.quantity,
                    OrderType.SL_M,
                    pos.stop_price,
                    pos.stop_price,
                )  # fmt: skip
            )
            if result.accepted and result.order is not None:
                pos.stop_coid = result.order.client_order_id
                self._stale_stops.discard(key)
            else:
                self._alert("exit_stop_not_placed", f"{key}: {result.status} {result.message}")
        self._save()

    async def flatten(self, reason: str = "flatten") -> None:
        """Exit every managed position at market (kill-switch FLATTEN)."""
        for pos in list(self._state.positions.values()):
            await self._exit_all(pos, IntentReason.FLATTEN, reason, None)
        self._save()

    async def stand_down(self, instrument_key: str, product: Product) -> None:
        """The kill switch is flattening this position: cancel its stop and stop managing it
        (no stop, target or partial is placed for it again until ``reconcile`` finds no exit
        working and takes it back).
        """
        key = f"{instrument_key}|{product.value}"
        pos = self._state.positions.get(key)
        if pos is None:
            return
        await self._cancel_stop(pos)
        pos.closing = True
        self._stale_stops.discard(key)
        self._save()

    # -- actions --------------------------------------------------------------------------------------

    async def _exit_all(
        self, pos: ManagedPosition, reason: IntentReason, leg: str, price: Decimal | None
    ) -> None:
        await self._cancel_stop(pos)
        result = await self._oms.submit(
            self._intent(
                pos, leg, IntentKind.CLOSE, reason, pos.quantity, OrderType.MARKET, None, price
            )
        )
        if result.accepted:
            pos.closing = True
        else:
            self._alert(
                "exit_not_submitted", f"{pos.key} {reason}: {result.status} {result.message}"
            )
            self._stale_stops.add(pos.key)  # put protection back

    async def _exit_partial(self, pos: ManagedPosition, price: Decimal) -> None:
        lot = pos.instrument.lot_size
        qty = int(pos.quantity * self.policy.partial_fraction) // lot * lot
        if qty <= 0 or qty >= pos.quantity:
            pos.partial_taken = True  # too small to split: keep the whole position on the stop
            return
        await self._cancel_stop(pos)
        result = await self._oms.submit(
            self._intent(
                pos,
                "partial",
                IntentKind.REDUCE,
                IntentReason.PARTIAL,
                qty,
                OrderType.MARKET,
                None,
                price,
            )
        )
        if not result.accepted:
            self._alert(
                "exit_not_submitted", f"{pos.key} partial: {result.status} {result.message}"
            )
        pos.partial_taken = True
        self._stale_stops.add(pos.key)  # the remainder needs its stop back

    async def _cancel_stop(self, pos: ManagedPosition) -> None:
        if pos.stop_coid is not None:
            await self._oms.cancel(pos.stop_coid)
            pos.stop_coid = None

    def _intent(
        self,
        pos: ManagedPosition,
        leg: str,
        kind: IntentKind,
        reason: IntentReason,
        qty: int,
        order_type: OrderType,
        trigger: Decimal | None,
        price: Decimal | None,
    ) -> OrderIntent:
        return OrderIntent(
            intent_id=intent_id(
                book_id=self._oms.book_id,
                strategy=pos.strategy,
                instrument_key=pos.instrument.key,
                signal_bar_date=pos.signal_bar_date,
                leg=leg,
            ),
            decision_id=new_id(),
            parent_decision_id=pos.entry_decision_id,
            book_id=self._oms.book_id,
            strategy=pos.strategy,
            instrument=pos.instrument,
            side=Side.SELL,
            kind=kind,
            quantity=qty,
            order_type=order_type,
            product=pos.product,
            trigger_price=trigger,
            reduce_only=True,
            decision_price=price or pos.stop_price,
            decision_ts=self._clock.now(),
            reason=reason,
            source=IntentSource.EXIT_MANAGER,
        )

    # -- restart reconciliation (book <-> exit manager) -------------------------------------------------

    async def reconcile(self, *, atr: Mapping[str, Decimal]) -> list[str]:
        """Adopt book positions without exits (if an ATR is known), drop managed ghosts."""
        now = self._clock.now()
        notes: list[str] = []
        book = {
            f"{p.instrument_key}|{p.product.value}": p
            for p in self._oms.book.positions(now)
            if p.quantity != 0
        }
        for key in list(self._state.positions):
            if key not in book:
                del self._state.positions[key]
                notes.append(f"dropped managed {key}: the book holds nothing")
        for key, position in book.items():
            pos = self._state.positions.get(key)
            if pos is not None:
                if pos.quantity != position.quantity:
                    notes.append(f"{key}: managed {pos.quantity} -> book {position.quantity}")
                    pos.quantity = position.quantity
                    self._stale_stops.add(key)
                if pos.closing and not self._exit_working(pos):  # e.g. a flatten was abandoned
                    pos.closing = False
                    self._stale_stops.add(key)
                    notes.append(f"{key}: no exit working - managing it again")
                continue
            if position.quantity < 0:
                notes.append(f"{key}: SHORT {position.quantity} in a long-only book - flatten it")
                continue
            instrument_key = position.instrument_key
            known_atr = atr.get(instrument_key)
            instrument = self._instrument_for(instrument_key)
            if known_atr is None or instrument is None or position.avg_price is None:
                notes.append(f"{key}: held {position.quantity} with NO exits (no ATR/instrument)")
                continue
            stop, target = self._levels(position.avg_price, known_atr, instrument)
            day = now.astimezone(IST).date()
            self._state.positions[key] = ManagedPosition(
                instrument=instrument,
                product=position.product,
                strategy="adopted",
                entry_decision_id=new_id(),
                signal_bar_date=day,
                entry_price=position.avg_price,
                quantity=position.quantity,
                stop_price=stop,
                target_price=target,
                r_distance=position.avg_price - stop,
                atr=known_atr,
                entered_on=day,
                highest_close=position.avg_price,
            )
            self._stale_stops.add(key)
            notes.append(f"adopted {key}: {position.quantity} @ {position.avg_price}, stop {stop}")
        for note in notes:
            self._alert(
                "exit_reconcile",
                note,
                level="CRITICAL" if "NO exits" in note or "SHORT" in note else "WARNING",
            )
        await self.refresh_stops()
        self._save()
        return notes

    def _exit_working(self, pos: ManagedPosition) -> bool:
        return any(
            o.intent.reduce_only
            and o.intent.instrument.key == pos.instrument.key
            and o.intent.product is pos.product
            and not o.status.is_terminal
            for o in self._oms.orders.values()
        )

    def _live_stop(self, pos: ManagedPosition) -> Order | None:
        for order in self._oms.orders.values():
            intent = order.intent
            if (
                intent.reduce_only
                and intent.order_type is OrderType.SL_M
                and intent.instrument.key == pos.instrument.key
                and intent.product is pos.product
                and not order.status.is_terminal
            ):
                return order
        return None

    def _instrument_for(self, instrument_key: str) -> Instrument | None:
        for order in self._oms.orders.values():
            if order.intent.instrument.key == instrument_key:
                return order.intent.instrument
        return None

    # -- helpers ---------------------------------------------------------------------------------------

    def _alert(self, key: str, message: str, *, level: str = "WARNING") -> None:
        logger.warning("%s: %s", key, message)
        self._sink.emit(Alert(level=level, key=key, message=message), source="exit_manager")

    def _save(self) -> None:
        self._store.save(self._state.model_dump_json(), self._clock.now())
