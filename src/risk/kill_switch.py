"""
Kill switches (plan M4.4; audit §L.4).

:class:`KillSwitchRegistry` holds one Global, one Broker and one switch per Strategy, each
``ARMED`` < ``HALT_NEW`` < ``FLATTEN``, persisted with ``{state, reason, actor, ts}``. Switches
**latch**: a trip can only escalate them, and only :meth:`~KillSwitchRegistry.resume` (an
operator, the API) puts one back to ARMED. Every change emits ``KillSwitchChanged``. The only
automatic re-arm is a daily-loss trip at the next IST day, and only when
``RiskLimits.rearm_on_new_day`` is set.

Tripped by the monitor's risk tick (:class:`~src.risk.state.Breach`), by a ``HALT`` file in the
environment's state directory (checked every tick; its content may say ``FLATTEN``), and by the
API (M9). HALT_NEW blocks opens (the RiskEngine reads :meth:`kill_state`); FLATTEN additionally
has the :class:`Flattener` exit every position **through ``OMS.submit``** with reduce-only
FLATTEN intents, retried every tick until flat and escalated to a CRITICAL alert after
``flatten_escalate_after`` attempts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.config.limits import RiskLimits
from src.domain.clock import Clock, now_ist
from src.domain.events import Alert, KillSwitchChanged
from src.domain.ids import intent_id, new_id
from src.domain.sink import EventSink
from src.domain.types import (
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    KillScope,
    KillState,
    KillSwitchState,
    OrderIntent,
    OrderType,
    Position,
    ReasonCode,
    Side,
)
from src.oms.exit_manager import ExitManager
from src.oms.oms import OMS
from src.risk.marks import owner as position_owner
from src.risk.state import Breach
from src.store.kv import MemoryStateStore, StateStore

logger = logging.getLogger(__name__)

ARMED, HALT_NEW, FLATTEN = KillSwitchState.ARMED, KillSwitchState.HALT_NEW, KillSwitchState.FLATTEN
_RANK = {ARMED: 0, HALT_NEW: 1, FLATTEN: 2}
GLOBAL = "global"
HALT_FILE_CODE = "HALT_FILE"


class SwitchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: KillSwitchState = ARMED
    reason: str = ""
    actor: str = ""
    ts: datetime | None = None
    code: str | None = None  # what tripped it: a ReasonCode, HALT_FILE, or an API reason
    ist_date: date | None = None


class _Switches(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_switch: SwitchRecord = SwitchRecord()
    broker: SwitchRecord = SwitchRecord()
    strategies: dict[str, SwitchRecord] = {}


class KillSwitchRegistry:
    def __init__(
        self,
        *,
        book_id: str,
        limits: RiskLimits,
        clock: Clock,
        sink: EventSink,
        state_store: StateStore | None = None,
        halt_file: Path | None = None,
    ) -> None:
        self.book_id = book_id
        self.limits = limits
        self.halt_file = halt_file
        self._clock = clock
        self._sink = sink
        self._store = state_store or MemoryStateStore()
        saved = self._store.load()
        self._switches = _Switches.model_validate_json(saved) if saved else _Switches()

    # -- reading ------------------------------------------------------------------------------------

    def record(self, scope: KillScope, name: str = GLOBAL) -> SwitchRecord:
        if scope is KillScope.GLOBAL:
            return self._switches.global_switch
        if scope is KillScope.BROKER:
            return self._switches.broker
        return self._switches.strategies.get(name, SwitchRecord())

    def state(self, scope: KillScope, name: str = GLOBAL) -> KillSwitchState:
        return self.record(scope, name).state

    def kill_state(self) -> KillState:
        return KillState(
            global_state=self._switches.global_switch.state,
            broker=self._switches.broker.state,
            strategies={n: r.state for n, r in self._switches.strategies.items()},
        )

    def flattening(self) -> Sequence[str | None]:
        """What must be flattened: ``[None]`` for everything, else the strategies' names."""
        if self._switches.global_switch.state is FLATTEN:
            return [None]
        return sorted(n for n, r in self._switches.strategies.items() if r.state is FLATTEN)

    # -- changing ----------------------------------------------------------------------------------

    def trip(
        self,
        scope: KillScope,
        target: KillSwitchState,
        *,
        reason: str,
        actor: str,
        name: str = GLOBAL,
        code: str | None = None,
    ) -> bool:
        """Escalate a switch to ``target`` (never de-escalates). True when it changed."""
        if target is ARMED:
            raise ValueError("trip() only escalates; resume() re-arms")
        current = self.record(scope, name)
        if _RANK[target] <= _RANK[current.state]:
            return False
        self._set(scope, name, current, target, reason=reason, actor=actor, code=code)
        level = "CRITICAL" if target is FLATTEN or scope is KillScope.BROKER else "WARNING"
        self._sink.emit(
            Alert(level=level, key=f"kill_{scope}_{name}",
                  message=f"{scope} kill switch {name}: {current.state} -> {target} ({reason})"),
            source="risk",
        )  # fmt: skip
        return True

    def resume(self, scope: KillScope, *, actor: str, reason: str, name: str = GLOBAL) -> bool:
        """Manual re-arm. Refused for the global switch while the HALT file is still present."""
        current = self.record(scope, name)
        if current.state is ARMED:
            return False
        if scope is KillScope.GLOBAL and self.halt_file is not None and self.halt_file.exists():
            raise RuntimeError(f"remove {self.halt_file} before resuming the global switch")
        self._set(scope, name, current, ARMED, reason=reason, actor=actor, code=None)
        return True

    def apply(self, breaches: Iterable[Breach]) -> list[Breach]:
        """Act on the risk tick's breaches; returns those that changed a switch."""
        changed = []
        for b in breaches:
            target = FLATTEN if b.action == "FLATTEN" else HALT_NEW
            if self.trip(b.scope, target, reason=b.message, actor="monitor", name=b.name,
                         code=b.code.value):  # fmt: skip
                changed.append(b)
        return changed

    def check_halt_file(self) -> bool:
        """A ``HALT`` file trips the global switch (to FLATTEN if the file says so)."""
        if self.halt_file is None or not self.halt_file.exists():
            return False
        try:
            text = self.halt_file.read_text(encoding="utf-8", errors="replace").strip().upper()
        except OSError:
            text = ""  # unreadable still halts
        target = FLATTEN if text.startswith("FLATTEN") else HALT_NEW
        return self.trip(KillScope.GLOBAL, target, reason=f"{self.halt_file.name} file present",
                         actor="halt_file", code=HALT_FILE_CODE)  # fmt: skip

    def new_day(self) -> list[str]:
        """At a new IST day, re-arm daily-loss trips from earlier days - only if configured."""
        if not self.limits.rearm_on_new_day:
            return []
        today = now_ist(self._clock).date()
        rearmed = []
        daily = {ReasonCode.PF_DAILY_LOSS_MTM.value, ReasonCode.STR_DAILY_LOSS.value}
        candidates: list[tuple[KillScope, str]] = [(KillScope.GLOBAL, GLOBAL)]
        candidates += [(KillScope.STRATEGY, n) for n in self._switches.strategies]
        for scope, name in candidates:
            rec = self.record(scope, name)
            if (
                rec.state is not ARMED
                and rec.code in daily
                and rec.ist_date
                and rec.ist_date < today
            ):
                self._set(scope, name, rec, ARMED, reason=f"new IST day after {rec.code}",
                          actor="rearm", code=None)  # fmt: skip
                rearmed.append(name)
        return rearmed

    def _set(
        self,
        scope: KillScope,
        name: str,
        current: SwitchRecord,
        target: KillSwitchState,
        *,
        reason: str,
        actor: str,
        code: str | None,
    ) -> None:
        now = self._clock.now()
        record = SwitchRecord(state=target, reason=reason, actor=actor, ts=now, code=code,
                              ist_date=now_ist(self._clock).date())  # fmt: skip
        if scope is KillScope.GLOBAL:
            self._switches.global_switch = record
        elif scope is KillScope.BROKER:
            self._switches.broker = record
        else:
            self._switches.strategies[name] = record
        self._store.save(self._switches.model_dump_json(), now)  # persisted before announced
        self._sink.emit(
            KillSwitchChanged(book_id=self.book_id, scope=scope, name=name,
                              previous=current.state, current=target, reason=reason, actor=actor),
            source="risk",
        )  # fmt: skip
        log = logger.warning if target is not ARMED else logger.info
        log("kill switch %s/%s: %s -> %s by %s (%s)", scope, name, current.state, target, actor,
            reason)  # fmt: skip


class _FlattenState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: dict[str, int] = {}  # "<ist date>|<instrument>|<product>" -> orders sent
    escalated: set[str] = set()


class Flattener:
    """Exits positions at market through the OMS until the book (or a strategy) is flat."""

    def __init__(
        self,
        *,
        oms: OMS,
        clock: Clock,
        sink: EventSink,
        limits: RiskLimits,
        exit_manager: ExitManager | None = None,
        instruments: Mapping[str, Instrument] | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._oms = oms
        self._instruments = instruments or {}
        self._clock = clock
        self._sink = sink
        self._limits = limits
        self._exits = exit_manager
        self._store = state_store or MemoryStateStore()
        saved = self._store.load()
        self._state = _FlattenState.model_validate_json(saved) if saved else _FlattenState()

    async def step(self, marks: Mapping[str, Decimal], strategy: str | None = None) -> int:
        """One attempt per open position (all, or ``strategy``'s); returns how many remain."""
        now = self._clock.now()
        today = now_ist(self._clock).date()
        remaining = 0
        for position in self._oms.book.positions(now):
            if position.quantity == 0:
                continue
            owner = position_owner(self._oms.book, position)
            if strategy is not None and owner != strategy:
                continue
            remaining += 1
            if self._flatten_in_flight(position):
                continue  # a market order is working; check again next tick
            await self._release(position)
            key = f"{today.isoformat()}|{position.instrument_key}|{position.product.value}"
            attempt = self._state.attempts.get(key, 0) + 1
            self._state.attempts[key] = attempt
            self._save()  # the attempt number is spent before the order exists
            result = await self._oms.submit(self._intent(position, owner, attempt, today, marks))
            if not result.accepted:
                logger.warning("flatten %s attempt %d: %s %s", position.instrument_key, attempt,
                               result.status, result.message)  # fmt: skip
            if attempt >= self._limits.flatten_escalate_after and key not in self._state.escalated:
                self._state.escalated.add(key)
                self._save()
                self._sink.emit(
                    Alert(level="CRITICAL", key="flatten_escalated",
                          message=f"{position.instrument_key} still open after {attempt} flatten "
                                  f"attempts ({result.status}: {result.message}) - act manually"),
                    source="risk",
                )  # fmt: skip
        return remaining

    def _flatten_in_flight(self, position: Position) -> bool:
        return any(
            o.intent.kind is IntentKind.FLATTEN
            and o.intent.instrument.key == position.instrument_key
            and o.intent.product is position.product
            and not o.status.is_terminal
            for o in self._oms.orders.values()
        )

    async def _release(self, position: Position) -> None:
        """Free the quantity held by resting exits (stops, targets) so the flatten can take it."""
        if self._exits is not None:
            await self._exits.stand_down(position.instrument_key, position.product)
        for order in list(self._oms.orders.values()):
            intent = order.intent
            if (
                intent.reduce_only
                and intent.instrument.key == position.instrument_key
                and intent.product is position.product
                and not order.status.is_terminal
            ):
                await self._oms.cancel(order.client_order_id)

    def _intent(
        self,
        position: Position,
        owner: str,
        attempt: int,
        today: date,
        marks: Mapping[str, Decimal],
    ) -> OrderIntent:
        instrument = self._instrument(position.instrument_key)
        mark = marks.get(position.instrument_key) or position.avg_price or Decimal(1)
        strategy = owner if owner and ":" not in owner else "unknown"
        return OrderIntent(
            intent_id=intent_id(
                book_id=self._oms.book_id,
                strategy=strategy,
                instrument_key=position.instrument_key,
                signal_bar_date=today,
                leg=f"flatten-{attempt}",
            ),  # fmt: skip
            decision_id=new_id(),
            book_id=self._oms.book_id,
            strategy=strategy,
            instrument=instrument,
            side=Side.SELL if position.quantity > 0 else Side.BUY,
            kind=IntentKind.FLATTEN,
            quantity=abs(position.quantity),
            order_type=OrderType.MARKET,
            product=position.product,
            reduce_only=True,
            decision_price=mark,
            decision_ts=self._clock.now(),
            reason=IntentReason.FLATTEN,
            source=IntentSource.KILL_SWITCH,
        )

    def _instrument(self, instrument_key: str) -> Instrument:
        known = self._instruments.get(instrument_key)
        if known is not None:
            return known
        for order in self._oms.orders.values():
            if order.intent.instrument.key == instrument_key:
                return order.intent.instrument
        # Unknown to us (e.g. adopted from the broker): a MARKET exit needs no tick or band.
        exchange, series, symbol = instrument_key.split(":", 2)
        return Instrument(key=instrument_key, exchange=exchange, segment="CM", symbol=symbol,
                          series=series)  # fmt: skip

    def _save(self) -> None:
        self._store.save(self._state.model_dump_json(), self._clock.now())
