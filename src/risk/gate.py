"""
The OMS's pre-trade gate (plan M4.2; audit §L.1): every order - entries, exits, flattens and
operator orders - is evaluated by the :class:`~src.risk.engine.RiskEngine` against a **fresh**
:class:`~src.risk.snapshot.RiskSnapshot`, and the ``RiskDecision`` is persisted **before** the OMS
records ``OrderSubmitted`` and routes. There is no other path to the broker.

The snapshot reserves the capacity of orders that are still working (submitted, not yet filled),
so back-to-back submits cannot each spend the same headroom. The HALT file is re-checked on every
submit, so it blocks the very next order. :meth:`RiskGate.preview` gives the UI the same answer
without recording it; only the OMS's decision is binding.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.domain.calendar import CalendarCoverageError, NSECalendar
from src.domain.clock import Clock, now_ist
from src.domain.sink import EventSink
from src.domain.types import (
    Instrument,
    OrderIntent,
    OrderStatus,
    OrderType,
    ReasonCode,
    RiskDecision,
    SessionState,
    TypedEvent,
)
from src.engine.lifecycle import LifecycleConfig, build_schedule
from src.oms.oms import OMS, GateResult
from src.risk.engine import RiskEngine
from src.risk.events import EventBlock, EventRules, event_blocks
from src.risk.kill_switch import KillSwitchRegistry
from src.risk.marks import open_positions, owner, unrealized_by_strategy
from src.risk.snapshot import (
    MarketFacts,
    PositionInfo,
    Reservations,
    RiskContext,
    RiskSnapshot,
)
from src.risk.state import DailyRiskTracker
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

MarketFactsSource = Callable[[Instrument], MarketFacts]  # fresh quote, ATR, ADV, data source


@dataclass
class SystemFlags:
    """System conditions the engine reads; set by whoever detects them."""

    llm_degraded: bool = False
    journal_durable: bool = True
    recon_drift: bool = False


class RiskGate:
    def __init__(
        self,
        *,
        engine: RiskEngine,
        oms: OMS,
        tracker: DailyRiskTracker,
        switches: KillSwitchRegistry,
        calendar: NSECalendar,
        clock: Clock,
        sink: EventSink,
        market: MarketFactsSource,
        environment: str,
        lifecycle: LifecycleConfig | None = None,
        instruments: Mapping[str, Instrument] | None = None,
        flags: SystemFlags | None = None,
        events: Callable[[], Iterable[TypedEvent]] | None = None,
        event_rules: EventRules | None = None,
    ) -> None:
        self.engine = engine
        self.flags = flags or SystemFlags()
        self._oms = oms
        self._tracker = tracker
        self._switches = switches
        self._calendar = calendar
        self._clock = clock
        self._sink = sink
        self._market = market
        self._environment = environment
        self._lifecycle = lifecycle or LifecycleConfig()
        self._instruments = dict(instruments or {})
        self._events = events
        self._event_rules = event_rules or EventRules()

    async def __call__(self, intent: OrderIntent, quantity: int | None) -> GateResult:
        self._switches.check_halt_file()  # takes effect on this very order
        decision = self._decide(intent, quantity)
        self._sink.emit(decision, source="risk")  # persisted before the OMS routes
        if decision.qty_approved <= 0:
            reasons = ", ".join(r.code.value for r in decision.reasons)
            return GateResult(0, f"{decision.outcome}: {reasons}")
        return GateResult(decision.qty_approved, str(decision.outcome))

    def preview(self, intent: OrderIntent, quantity: int | None = None) -> RiskDecision:
        """What the engine would decide now, for explanations in the UI (nothing recorded)."""
        return self._decide(intent, quantity)

    def _decide(self, intent: OrderIntent, quantity: int | None) -> RiskDecision:
        snapshot = self.snapshot(intent)
        reserved = self._working(snapshot)
        return self.engine.evaluate(intent, snapshot, quantity=quantity, reserved=reserved).decision

    # -- the snapshot ------------------------------------------------------------------------------

    def snapshot(self, intent: OrderIntent) -> RiskSnapshot:
        now = self._clock.now()
        book = self._oms.book
        orders = list(self._oms.orders.values())
        working = [o for o in orders if not o.status.is_terminal]

        instruments = {intent.instrument.key: intent.instrument}
        for order in orders:
            instruments.setdefault(order.intent.instrument.key, order.intent.instrument)
        for key, instrument in self._instruments.items():
            instruments.setdefault(key, instrument)

        positions = open_positions(book, now)
        wanted = {intent.instrument.key, *(p.instrument_key for p in positions)}
        wanted |= {o.intent.instrument.key for o in working}
        market = {
            key: self._market(instruments[key]) if key in instruments else MarketFacts()
            for key in wanted
        }
        marks: dict[str, Decimal] = {}
        for p in positions:
            quote = market[p.instrument_key].quote
            marks[p.instrument_key] = (
                Decimal(str(quote.ltp)) if quote is not None else p.avg_price or Decimal(0)
            )

        stops = {  # the resting protective stop per instrument, if any
            o.intent.instrument.key: o.intent.trigger_price
            for o in working
            if o.intent.reduce_only and o.intent.order_type in (OrderType.SL_M, OrderType.SL)
        }
        infos = {
            p.instrument_key: PositionInfo(
                instrument_key=p.instrument_key,
                quantity=p.quantity,
                avg_price=p.avg_price or Decimal(0),
                mark=marks[p.instrument_key],
                sector=instruments[p.instrument_key].sector
                if p.instrument_key in instruments
                else None,
                strategy=owner(book, p),
                stop_price=stops.get(p.instrument_key),
            )
            for p in positions
        }

        equity = book.equity(marks)
        state = self._tracker.state
        today = now_ist(self._clock).date()
        current = state is not None and state.ist_date == today
        sod = state.sod_equity if state is not None and current else equity
        peak = max(equity, state.peak_equity) if state is not None else equity

        trading_day, session_open, entry_window = False, False, False
        try:
            trading_day = self._calendar.is_trading_day(today)
            session_open = self._calendar.is_market_open(now)
            schedule = build_schedule(self._calendar, today, self._lifecycle)
            entry_window = schedule is not None and (
                schedule.state_at(now) is SessionState.ENTRY_WINDOW
            )
        except CalendarCoverageError:
            logger.warning("no calendar coverage for %s: the market is treated as closed", today)

        return RiskSnapshot(
            now=now,
            environment=self._environment,
            equity=equity,
            cash=book.cash,
            sod_equity=sod,
            peak_equity=peak,
            positions=infos,
            market=market,
            strategies=self._tracker.strategy_stats(unrealized_by_strategy(book, marks, now)),
            kill=self._switches.kill_state(),
            open_orders=len(working),
            orders_last_min=self._tracker.orders_last_min(),
            rejects_in_window=self._tracker.rejects_in_window(),
            entries_today=state.entries if state is not None and current else 0,
            symbols_entered_today=frozenset(state.symbols_entered)
            if state is not None and current
            else frozenset(),
            symbols_exited_today=frozenset(state.symbols_exited)
            if state is not None and current
            else frozenset(),
            unknown_order_keys=frozenset(
                o.intent.instrument.key for o in orders if o.status is OrderStatus.UNKNOWN
            ),
            is_trading_day=trading_day,
            is_weekend=today.weekday() >= 5,
            session_open=session_open,
            entry_window=entry_window,
            llm_degraded=self.flags.llm_degraded,
            journal_durable=self.flags.journal_durable,
            recon_drift=self.flags.recon_drift,
            event_blocks=self._event_blocks(intent, now),
        )

    def _event_blocks(
        self, intent: OrderIntent, now: datetime
    ) -> dict[str, tuple[EventBlock, ...]]:
        if self._events is None:
            return {}
        try:
            return event_blocks(self._events(), calendar=self._calendar,
                                rules=self._event_rules, now=now)  # fmt: skip
        except Exception as exc:  # unknown events: no new entry in this instrument (fail closed)
            logger.exception("event calendar unavailable")
            today = now.astimezone(IST).date()
            unknown = EventBlock(ReasonCode.EVT_RESULTS_WINDOW, today, today, "unknown",
                                 f"event calendar unavailable: {type(exc).__name__}")  # fmt: skip
            return {intent.instrument.key: (unknown,)}

    def _working(self, snapshot: RiskSnapshot) -> Reservations:
        """Capacity held by submitted entries that have not filled yet."""
        reserved = Reservations()
        for order in self._oms.orders.values():
            if order.status.is_terminal or order.intent.kind.reduces_risk:
                continue
            ctx = RiskContext(order.intent, snapshot, self.engine.limits, reserved, None)
            reserved.add(ctx, order.remaining_qty, counted=False)
        return reserved
