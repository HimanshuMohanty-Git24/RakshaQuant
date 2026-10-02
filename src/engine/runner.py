"""
The engine runner (plan M5.6, M8.1-8.2): the trading day, end to end, for one or more paired
**books**. The same code runs live (wall clock, YFinance) and in replay (ReplayClock, a tape).

:func:`build_engine` wires the v2 components on one event store. **Shared** by every book: the
:class:`MarketService` (quotes, history, features), the signals, the regime, the announcements
and the session lifecycle. **Per book** (a :class:`Book`): its own ``SimulatedBroker`` state,
PositionBook/OMS behind its own RiskGate, ExitManager, DailyRiskTracker, kill switches,
Flattener and RiskMonitor - so books A (no advisor), B (typed veto) and C (LLM veto) never share
cash, capacity or limits. One ``decision_id`` per signal is shared across books; ``book_id`` is
on every event. On a restart each book's OMS is restored from the store's events and every
stateful component reloads its own kv state, so CNC books carry over from day to day.

:meth:`Engine.run` drives the session state machine (:class:`SessionLifecycle`):

* **PRE_OPEN** - load the day's history (universe ∪ held ∪ NIFTY), compute the regime, start each
  book's risk day, reconcile each book, backfill announcements.
* **OPEN / ENTRY_WINDOW / MONITOR** (whichever comes first, so a late start still does it) - set
  the fill model's liquidity, re-place each book's DAY stops (trailing, time exits), and start the
  market-data, per-book monitor and reconciler, and announcements loops.
* **ENTRY_WINDOW** - one decision cycle (every book).
* **CLOSE** - stop the loops, expire live DAY orders, a last risk tick per book.
* **REPORT** - each book's mark-to-market (the full daily report: :mod:`src.evaluation`).

The shadow ledger (:mod:`src.evaluation.shadow_ledger`) follows every signal on the shared tape.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from src.brokers.simulated.broker import SimulatedBroker
from src.brokers.simulated.costs import NSECostSchedule
from src.brokers.simulated.fill_model import FillModelConfig, MarketContext
from src.config.limits import RiskLimits
from src.decision.engine import Advisor, BookTarget, CycleResult, DecisionConfig, DecisionEngine
from src.domain.calendar import NSECalendar
from src.domain.clock import Clock
from src.domain.events import Alert, MarkToMarket, OrderSubmitted
from src.domain.types import Instrument, Quote, Regime, SessionState, TypedEvent
from src.engine.lifecycle import LifecycleConfig, LifecycleHooks, Schedule, SessionLifecycle
from src.engine.market import HistorySource, MarketService, QuoteSource
from src.engine.tasks import (
    ANNOUNCEMENTS,
    MARKET_DATA,
    MONITOR,
    MONITOR_INTERVAL_S,
    RECONCILE_INTERVAL_S,
    RECONCILER,
    TaskGroup,
)
from src.evaluation.shadow_ledger import ShadowLedger
from src.features.regime import RegimeConfig, compute_regime
from src.features.technical import as_date
from src.oms.exit_manager import ExitManager
from src.oms.oms import OMS
from src.oms.position_book import PositionBook
from src.risk.engine import RiskEngine
from src.risk.events import EventRules
from src.risk.gate import RiskGate
from src.risk.kill_switch import Flattener, KillSwitchRegistry
from src.risk.marks import open_positions
from src.risk.monitor import RiskMonitor
from src.risk.state import DailyRiskTracker
from src.store.event_store import EventStore
from src.store.kv import KVRecordStore, KVStateStore
from src.store.sink import StoreSink
from src.strategies import Strategy
from src.strategies.policy import TradePolicy, TradePolicyConfig

logger = logging.getLogger(__name__)

OMS_EVENTS = (
    "OrderSubmitted", "OrderAcked", "OrderRejected", "OrderUnknown", "OrderCancelled",
    "OrderExpired", "FillReceived",
)  # fmt: skip
_IN_SESSION = frozenset({SessionState.OPEN, SessionState.ENTRY_WINDOW, SessionState.MONITOR})


class AnnouncementSource(Protocol):
    """Polls corporate announcements and classifies them."""

    @property
    def interval_s(self) -> float: ...

    async def poll(self, *, force: bool = False) -> Sequence[TypedEvent]: ...


@dataclass(frozen=True)
class EngineConfig:
    environment: str
    books: tuple[str, ...] = ("A",)
    starting_cash: Decimal = Decimal(1_000_000)  # per book
    halt_file: Path | None = None
    costs: NSECostSchedule = field(default_factory=NSECostSchedule.from_yaml)
    fill_model: FillModelConfig = field(default_factory=FillModelConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    policy: TradePolicyConfig = field(default_factory=TradePolicyConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    decision: DecisionConfig | None = None  # default: enabled = the risk limits' strategies
    event_rules: EventRules = field(default_factory=EventRules.from_yaml)

    def __post_init__(self) -> None:
        if not self.books or len(set(self.books)) != len(self.books):
            raise ValueError("books must be non-empty and distinct")

    @property
    def book_id(self) -> str:
        """The primary book (the first)."""
        return self.books[0]


@dataclass
class Book:
    book_id: str
    broker: SimulatedBroker
    oms: OMS
    gate: RiskGate
    exits: ExitManager
    tracker: DailyRiskTracker
    switches: KillSwitchRegistry
    monitor: RiskMonitor


@dataclass
class Engine:
    config: EngineConfig
    clock: Clock
    calendar: NSECalendar
    store: EventStore
    sink: StoreSink
    market: MarketService
    books: dict[str, Book]
    decision: DecisionEngine
    lifecycle: SessionLifecycle
    tasks: TaskGroup
    announcements: AnnouncementSource | None = None
    ledger: ShadowLedger | None = None
    regime: Regime | None = None
    cycles: list[CycleResult] = field(default_factory=list)
    _session_started: bool = False

    # -- the primary book (single-book callers: the CLI/web view model, tests) -------------------

    @property
    def primary(self) -> Book:
        return self.books[self.config.book_id]

    @property
    def broker(self) -> SimulatedBroker:
        return self.primary.broker

    @property
    def oms(self) -> OMS:
        return self.primary.oms

    @property
    def gate(self) -> RiskGate:
        return self.primary.gate

    @property
    def exits(self) -> ExitManager:
        return self.primary.exits

    @property
    def tracker(self) -> DailyRiskTracker:
        return self.primary.tracker

    @property
    def switches(self) -> KillSwitchRegistry:
        return self.primary.switches

    @property
    def monitor(self) -> RiskMonitor:
        return self.primary.monitor

    def set_advisor(self, book_id: str, advisor: Advisor | None) -> None:
        self.decision.books[book_id].advisor = advisor

    def events_for(self, instrument_key: str) -> list[TypedEvent]:
        """An instrument's classified announcements (for Book B's typed veto)."""
        rows = self.store.query(
            "SELECT payload FROM typed_events WHERE instrument_key = ?", (instrument_key,)
        )
        return [TypedEvent.model_validate_json(r["payload"]) for r in rows]

    # -- the day -------------------------------------------------------------------------------

    async def run(self) -> int:
        for book in self.books.values():
            await book.oms.start()
        try:
            return await self.lifecycle.run()
        finally:
            await self.tasks.stop()
            for book in self.books.values():
                await book.oms.stop()

    async def on_pre_open(self, schedule: Schedule) -> None:
        day = schedule.day
        previous = self.calendar.previous_trading_day(day)
        await self.market.load_history(day, previous)
        self.regime = self._compute_regime(day)
        if self.ledger is not None:
            self.ledger.settle_alpha(self._index_closes())
        for book in self.books.values():
            book.monitor.start_day()
            result = await book.oms.reconcile()
            book.gate.flags.recon_drift = not result.in_sync
        if self.announcements is not None:
            try:  # the overnight backfill; a feed outage never blocks the session
                await self.announcements_step(force=True)
            except Exception as exc:
                logger.exception("announcement backfill failed")
                self.sink.emit(Alert(level="WARNING", key="announcements_backfill_failed",
                                     message=f"{type(exc).__name__}: {exc}"),
                               source="engine")  # fmt: skip

    async def on_state(self, state: SessionState, schedule: Schedule) -> None:
        if state in _IN_SESSION and not self._session_started:
            self._session_started = True
            await self._open_session(schedule)
        if state is SessionState.ENTRY_WINDOW:
            await self.market.poll()  # fresh marks before deciding
            universe = [self.market.instruments[k] for k in sorted(self.market.instruments)]
            cycle = await self.decision.run_cycle(universe, regime=self.regime)
            self.cycles.append(cycle)
            if self.ledger is not None:
                atr = {}
                for signal in cycle.signals:
                    f = self.market.features(signal.instrument_key)
                    atr[signal.instrument_key] = Decimal(str(f.atr_14)) if f and f.atr_14 else None
                self.ledger.track(cycle.signals, atr)
        elif state is SessionState.CLOSE:
            await self.tasks.stop()
            for book in self.books.values():
                book.broker.expire_session(self.clock.now())
                await book.monitor.tick()
        elif state is SessionState.REPORT:
            for book in self.books.values():
                self._report(book)

    async def _open_session(self, schedule: Schedule) -> None:
        market = self.market
        contexts = {}
        atr: dict[str, Decimal] = {}
        closes: dict[str, Decimal] = {}
        for key in market.instruments:
            f = market.features(key)
            if f is None:
                continue
            contexts[key] = MarketContext(adv_inr=f.adv20_inr, adv_shares=f.adv20_shares,
                                          sigma_daily=f.sigma_daily)  # fmt: skip
            if f.atr_14:
                atr[key] = Decimal(str(f.atr_14))
            closes[key] = Decimal(str(f.close))
        for book in self.books.values():
            book.broker.set_market_context(contexts)
        if self.ledger is not None:  # its time exits are due at the first quote of the day
            self.ledger.on_session_start(schedule.day, closes=closes, atr=atr)
        await market.poll()  # the first quotes of the day, before any exit is re-placed
        for book in self.books.values():
            await book.exits.on_session_start(schedule.day, atr=atr, closes=closes)

        self.tasks.start(MARKET_DATA, market.poll, lambda: market.next_delay_s)
        for book in self.books.values():
            self.tasks.start(f"{MONITOR}:{book.book_id}", _monitor_step(book),
                             lambda: MONITOR_INTERVAL_S)  # fmt: skip
            self.tasks.start(f"{RECONCILER}:{book.book_id}", _reconcile_step(book),
                             lambda: RECONCILE_INTERVAL_S)  # fmt: skip
        announcements = self.announcements
        if announcements is not None:
            self.tasks.start(ANNOUNCEMENTS, self.announcements_step,
                             lambda: announcements.interval_s)  # fmt: skip

    async def announcements_step(self, *, force: bool = False) -> None:
        """Ingest and classify; alert (never exit) on an adverse event for a held position."""
        if self.announcements is None:
            return
        events = await self.announcements.poll(force=force)
        now = self.clock.now()
        holders: dict[str, list[str]] = {}
        for book in self.books.values():
            for p in open_positions(book.oms.book, now):
                holders.setdefault(p.instrument_key, []).append(book.book_id)
        for event in events:
            books = holders.get(event.instrument_key)
            if books and self.config.event_rules.is_held_alert(event):
                self.sink.emit(
                    Alert(level="CRITICAL", key=f"held_adverse_event:{event.instrument_key}",
                          message=f"held {event.instrument_key} (books {', '.join(books)}): "
                                  f"{event.direction} {event.materiality} event - "
                                  f"{event.title[:160]}"),
                    source="engine",
                )  # fmt: skip

    def _index_closes(self) -> dict[date, float]:
        series = self.market.index_series()
        if series is None:
            return {}
        frame = series.raw()
        return {as_date(ts): float(c) for ts, c in zip(frame.index, frame["close"], strict=True)}

    def _compute_regime(self, day: object) -> Regime | None:
        series = self.market.index_series()
        if series is None:
            self.sink.emit(Alert(level="WARNING", key="regime_unavailable",
                                 message="no NIFTY history: regime unknown today"),
                           source="engine")  # fmt: skip
            return None
        try:
            reading = compute_regime(series.adjusted(), self.config.regime)
        except ValueError as exc:
            self.sink.emit(Alert(level="WARNING", key="regime_unavailable", message=str(exc)),
                           source="engine")  # fmt: skip
            return None
        assert self.lifecycle.schedule is not None
        self.sink.emit(reading.event(self.lifecycle.schedule.day), source="engine")
        return reading.label

    def _report(self, book: Book) -> None:
        marks = book.monitor.marks()
        position_book = book.oms.book
        now = self.clock.now()
        value = position_book.market_value(marks)
        unrealized = sum(
            ((marks[p.instrument_key] - (p.avg_price or Decimal(0))) * p.quantity
             for p in open_positions(position_book, now)),
            Decimal(0),
        )  # fmt: skip
        state = book.tracker.state
        equity = position_book.cash + value
        self.sink.emit(
            MarkToMarket(book_id=book.book_id, equity=equity, cash=position_book.cash,
                         positions_value=value, unrealized_pnl=unrealized,
                         day_pnl=equity - state.sod_equity if state else Decimal(0)),
            source="engine",
        )  # fmt: skip


def _monitor_step(book: Book) -> Callable[[], Awaitable[None]]:
    async def step() -> None:
        await book.oms.resolve_unknown()
        await book.monitor.tick()

    return step


def _reconcile_step(book: Book) -> Callable[[], Awaitable[None]]:
    async def step() -> None:
        result = await book.oms.reconcile()
        book.gate.flags.recon_drift = not result.in_sync

    return step


def build_engine(
    *,
    config: EngineConfig,
    clock: Clock,
    calendar: NSECalendar,
    store: EventStore,
    quotes: QuoteSource,
    history: HistorySource,
    universe: Sequence[Instrument],
    limits: RiskLimits,
    strategies: Mapping[str, Strategy] | None = None,
    announcements: AnnouncementSource | None = None,
    advisors: Mapping[str, Advisor | None] | None = None,
) -> Engine:
    sink = StoreSink(store, clock, "engine")
    held: dict[str, Instrument] = {}
    for book_id in config.books:
        held |= held_instruments(store, book_id)
    instruments = {i.key: i for i in universe} | held  # no position is ever unpriced
    market = MarketService(instruments=instruments, quotes=quotes, history=history, sink=sink)

    def stored_events() -> list[TypedEvent]:
        rows = store.query("SELECT payload FROM typed_events")
        return [TypedEvent.model_validate_json(r["payload"]) for r in rows]

    books: dict[str, Book] = {}
    for book_id in config.books:
        books[book_id] = _build_book(
            book_id, config=config, clock=clock, calendar=calendar, store=store, sink=sink,
            market=market, instruments=instruments, limits=limits, events=stored_events,
        )  # fmt: skip
        if held:
            logger.info("book %s restored; universe ∪ held = %d instruments", book_id,
                        len(instruments))  # fmt: skip

    chosen = dict(advisors or {})
    decision_config = config.decision or DecisionConfig(
        book_id=config.book_id, enabled=tuple(limits.enabled_strategies)
    )
    decision = DecisionEngine(
        config=decision_config, market=market, policy=TradePolicy(config.policy), clock=clock,
        sink=sink, strategies=strategies,
        books=[BookTarget(b.book_id, b.oms, b.exits, chosen.get(b.book_id)) for b in books.values()],
    )  # fmt: skip
    ledger = ShadowLedger(
        policy=config.policy.exit_policy(), costs=config.costs, calendar=calendar, clock=clock,
        sink=sink, notional=config.starting_cash * Decimal(str(limits.max_position_pct)),
        state_store=KVStateStore(store, "ledger", "shadow"),
    )  # fmt: skip
    market.add_listener(ledger.on_quote)
    engine = Engine(
        config=config, clock=clock, calendar=calendar, store=store, sink=sink, market=market,
        books=books, decision=decision, announcements=announcements, ledger=ledger,
        lifecycle=SessionLifecycle(clock=clock, calendar=calendar, sink=sink,
                                   config=config.lifecycle),
        tasks=TaskGroup(clock, sink),
    )  # fmt: skip
    engine.lifecycle.hooks = LifecycleHooks(on_pre_open=engine.on_pre_open,
                                            on_state=engine.on_state)  # fmt: skip
    return engine


def _build_book(
    book_id: str,
    *,
    config: EngineConfig,
    clock: Clock,
    calendar: NSECalendar,
    store: EventStore,
    sink: StoreSink,
    market: MarketService,
    instruments: Mapping[str, Instrument],
    limits: RiskLimits,
    events: Callable[[], Iterable[TypedEvent]],
) -> Book:
    broker = SimulatedBroker(
        book_id=book_id, instruments=instruments, clock=clock, calendar=calendar,
        costs=config.costs, starting_cash=config.starting_cash,
        state_store=KVRecordStore(store, f"broker:{book_id}"), config=config.fill_model,
    )  # fmt: skip
    position_book = PositionBook(book_id, config.starting_cash)
    oms = OMS(book_id=book_id, broker=broker, book=position_book, sink=sink, clock=clock)
    restored = oms.restore(store.read(types=list(OMS_EVENTS), book_id=book_id))
    if restored:
        logger.info("book %s: restored %d OMS events", book_id, restored)
    tracker = DailyRiskTracker(book_id=book_id, limits=limits, clock=clock, sink=sink,
                               state_store=KVStateStore(store, book_id, "daily_risk"))  # fmt: skip
    switches = KillSwitchRegistry(book_id=book_id, limits=limits, clock=clock, sink=sink,
                                  state_store=KVStateStore(store, book_id, "kill_switches"),
                                  halt_file=config.halt_file)  # fmt: skip
    oms.add_event_listener(tracker.on_event)
    exits = ExitManager(oms=oms, clock=clock, calendar=calendar, sink=sink,
                        policy=config.policy.exit_policy(),
                        state_store=KVStateStore(store, book_id, "exit_manager"))  # fmt: skip
    flattener = Flattener(oms=oms, clock=clock, sink=sink, limits=limits, exit_manager=exits,
                          instruments=instruments,
                          state_store=KVStateStore(store, book_id, "flatten"))  # fmt: skip

    async def to_broker(quote: Quote) -> None:
        broker.on_quote(quote)

    market.add_listener(to_broker)  # fills first, then this book's exits
    market.add_listener(exits.on_quote)
    gate = RiskGate(engine=RiskEngine(limits), oms=oms, tracker=tracker, switches=switches,
                    calendar=calendar, clock=clock, sink=sink, market=market.facts,
                    environment=config.environment, lifecycle=config.lifecycle,
                    instruments=instruments, events=events,
                    event_rules=config.event_rules)  # fmt: skip
    oms.use_gate(gate)
    monitor = RiskMonitor(book=position_book, tracker=tracker, switches=switches,
                          flattener=flattener, marks=market.marks, clock=clock, sink=sink)  # fmt: skip
    return Book(book_id, broker, oms, gate, exits, tracker, switches, monitor)


def held_instruments(store: EventStore, book_id: str) -> dict[str, Instrument]:
    """The instruments the book holds, from the positions projection (and the orders that opened
    them, for tick size, lot and band)."""
    rows = store.query(
        "SELECT instrument_key FROM positions WHERE book_id = ? AND quantity != 0", (book_id,)
    )
    keys = {str(r["instrument_key"]) for r in rows}
    found: dict[str, Instrument] = {}
    for event in store.read(types=["OrderSubmitted"], book_id=book_id):
        payload = event.payload
        if isinstance(payload, OrderSubmitted):
            instrument = payload.order.intent.instrument
            if instrument.key in keys:
                found[instrument.key] = instrument
    for key in sorted(keys - found.keys()):
        exchange, series, symbol = key.split(":", 2)
        found[key] = Instrument(key=key, exchange=exchange, segment="CM", symbol=symbol,
                                series=series)  # fmt: skip
    return found
