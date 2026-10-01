"""
The engine runner (plan M5.6): one book's trading day, end to end, replacing the legacy
``run_trading_session`` loop. The same code runs live (wall clock, YFinance) and in replay
(ReplayClock, a recorded tape).

:func:`build_engine` wires the v2 components on one event store:
``MarketService → DecisionEngine → OMS (RiskGate) → SimulatedBroker → ExitManager``, with the
DailyRiskTracker, kill switches, Flattener and RiskMonitor around them. On a restart the OMS is
restored from the store's events, and every stateful component reloads its own kv state, so a
CNC book carries over from day to day.

:meth:`Engine.run` drives the session state machine (:class:`SessionLifecycle`):

* **PRE_OPEN** - load the day's history (universe ∪ held ∪ NIFTY), compute the regime, start the
  risk day, reconcile with the broker.
* **OPEN / ENTRY_WINDOW / MONITOR** (whichever comes first, so a late start still does it) - set
  the fill model's liquidity, re-place the exits' DAY stops (trailing, time exits), and start the
  market-data, monitor and reconciler loops.
* **ENTRY_WINDOW** - one decision cycle.
* **CLOSE** - stop the loops, expire live DAY orders, a last risk tick.
* **REPORT** - the day's mark-to-market (the full daily report is M8).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from src.brokers.simulated.broker import SimulatedBroker
from src.brokers.simulated.costs import NSECostSchedule
from src.brokers.simulated.fill_model import FillModelConfig, MarketContext
from src.config.limits import RiskLimits
from src.decision.engine import CycleResult, DecisionConfig, DecisionEngine
from src.domain.calendar import NSECalendar
from src.domain.clock import Clock
from src.domain.events import Alert, MarkToMarket, OrderSubmitted
from src.domain.types import Instrument, Quote, Regime, SessionState
from src.engine.lifecycle import LifecycleConfig, LifecycleHooks, Schedule, SessionLifecycle
from src.engine.market import HistorySource, MarketService, QuoteSource
from src.engine.tasks import (
    MARKET_DATA,
    MONITOR,
    MONITOR_INTERVAL_S,
    RECONCILE_INTERVAL_S,
    RECONCILER,
    TaskGroup,
)
from src.features.regime import RegimeConfig, compute_regime
from src.oms.exit_manager import ExitManager
from src.oms.oms import OMS
from src.oms.position_book import PositionBook
from src.risk.engine import RiskEngine
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


@dataclass(frozen=True)
class EngineConfig:
    environment: str
    book_id: str = "A"
    starting_cash: Decimal = Decimal(1_000_000)
    halt_file: Path | None = None
    costs: NSECostSchedule = field(default_factory=NSECostSchedule.from_yaml)
    fill_model: FillModelConfig = field(default_factory=FillModelConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    policy: TradePolicyConfig = field(default_factory=TradePolicyConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    decision: DecisionConfig | None = None  # default: the book, with enabled = the risk limits'


@dataclass
class Engine:
    config: EngineConfig
    clock: Clock
    calendar: NSECalendar
    store: EventStore
    sink: StoreSink
    market: MarketService
    broker: SimulatedBroker
    oms: OMS
    gate: RiskGate
    exits: ExitManager
    tracker: DailyRiskTracker
    switches: KillSwitchRegistry
    monitor: RiskMonitor
    decision: DecisionEngine
    lifecycle: SessionLifecycle
    tasks: TaskGroup
    regime: Regime | None = None
    cycles: list[CycleResult] = field(default_factory=list)
    _session_started: bool = False

    # -- the day -------------------------------------------------------------------------------

    async def run(self) -> int:
        await self.oms.start()
        try:
            return await self.lifecycle.run()
        finally:
            await self.tasks.stop()
            await self.oms.stop()

    async def on_pre_open(self, schedule: Schedule) -> None:
        day = schedule.day
        previous = self.calendar.previous_trading_day(day)
        await self.market.load_history(day, previous)
        self.regime = self._compute_regime(day)
        self.monitor.start_day()
        result = await self.oms.reconcile()
        self.gate.flags.recon_drift = not result.in_sync

    async def on_state(self, state: SessionState, schedule: Schedule) -> None:
        if state in _IN_SESSION and not self._session_started:
            self._session_started = True
            await self._open_session(schedule)
        if state is SessionState.ENTRY_WINDOW:
            await self.market.poll()  # fresh marks before deciding
            universe = [self.market.instruments[k] for k in sorted(self.market.instruments)]
            self.cycles.append(await self.decision.run_cycle(universe, regime=self.regime))
        elif state is SessionState.CLOSE:
            await self.tasks.stop()
            self.broker.expire_session(self.clock.now())
            await self.monitor.tick()
        elif state is SessionState.REPORT:
            self._report()

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
        self.broker.set_market_context(contexts)
        await market.poll()  # the first quotes of the day, before any exit is re-placed
        await self.exits.on_session_start(schedule.day, atr=atr, closes=closes)

        async def monitor_step() -> None:
            await self.oms.resolve_unknown()
            await self.monitor.tick()

        async def reconcile_step() -> None:
            result = await self.oms.reconcile()
            self.gate.flags.recon_drift = not result.in_sync

        self.tasks.start(MARKET_DATA, market.poll, lambda: market.next_delay_s)
        self.tasks.start(MONITOR, monitor_step, lambda: MONITOR_INTERVAL_S)
        self.tasks.start(RECONCILER, reconcile_step, lambda: RECONCILE_INTERVAL_S)

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

    def _report(self) -> None:
        marks = self.monitor.marks()
        book = self.oms.book
        now = self.clock.now()
        value = book.market_value(marks)
        unrealized = sum(
            ((marks[p.instrument_key] - (p.avg_price or Decimal(0))) * p.quantity
             for p in open_positions(book, now)),
            Decimal(0),
        )  # fmt: skip
        state = self.tracker.state
        equity = book.cash + value
        self.sink.emit(
            MarkToMarket(book_id=self.config.book_id, equity=equity, cash=book.cash,
                         positions_value=value, unrealized_pnl=unrealized,
                         day_pnl=equity - state.sod_equity if state else Decimal(0)),
            source="engine",
        )  # fmt: skip


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
) -> Engine:
    book_id = config.book_id
    sink = StoreSink(store, clock, "engine")
    held = held_instruments(store, book_id)
    instruments = {i.key: i for i in universe} | held  # no position is ever unpriced
    broker = SimulatedBroker(
        book_id=book_id, instruments=instruments, clock=clock, calendar=calendar,
        costs=config.costs, starting_cash=config.starting_cash,
        state_store=KVRecordStore(store, f"broker:{book_id}"), config=config.fill_model,
    )  # fmt: skip
    book = PositionBook(book_id, config.starting_cash)
    oms = OMS(book_id=book_id, broker=broker, book=book, sink=sink, clock=clock)
    restored = oms.restore(store.read(types=list(OMS_EVENTS), book_id=book_id))
    if restored:
        logger.info("restored %d OMS events; holding %s", restored, sorted(held) or "nothing")
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
    market = MarketService(instruments=instruments, quotes=quotes, history=history, sink=sink)

    async def to_broker(quote: Quote) -> None:
        broker.on_quote(quote)

    market.add_listener(to_broker)
    market.add_listener(exits.on_quote)
    gate = RiskGate(engine=RiskEngine(limits), oms=oms, tracker=tracker, switches=switches,
                    calendar=calendar, clock=clock, sink=sink, market=market.facts,
                    environment=config.environment, lifecycle=config.lifecycle,
                    instruments=instruments)  # fmt: skip
    oms.use_gate(gate)
    monitor = RiskMonitor(book=book, tracker=tracker, switches=switches, flattener=flattener,
                          marks=market.marks, clock=clock, sink=sink)  # fmt: skip
    decision_config = config.decision or DecisionConfig(
        book_id=book_id, enabled=tuple(limits.enabled_strategies)
    )
    decision = DecisionEngine(config=decision_config, market=market, oms=oms, exits=exits,
                              policy=TradePolicy(config.policy), clock=clock, sink=sink,
                              strategies=strategies)  # fmt: skip
    engine = Engine(
        config=config, clock=clock, calendar=calendar, store=store, sink=sink, market=market,
        broker=broker, oms=oms, gate=gate, exits=exits, tracker=tracker, switches=switches,
        monitor=monitor, decision=decision,
        lifecycle=SessionLifecycle(clock=clock, calendar=calendar, sink=sink,
                                   config=config.lifecycle),
        tasks=TaskGroup(clock, sink),
    )  # fmt: skip
    engine.lifecycle.hooks = LifecycleHooks(on_pre_open=engine.on_pre_open,
                                            on_state=engine.on_state)  # fmt: skip
    return engine


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
