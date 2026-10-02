"""
Backtest the live engine itself (plan M11.1): every session of the period is an ordinary paper
session - the same lifecycle, features, strategies, TradePolicy, RiskEngine, OMS, simulated
broker, exit manager and NSE costs - fed the quotes its daily bars imply
(:mod:`src.backtesting.bars`) on a replay clock, one session after another on one event store.
State carries from day to day exactly as it does across real restarts (the OMS and positions are
rebuilt from events, the broker, exit manager, risk state and kill switches from the store).

So the backtest cannot drift from paper: there is no second implementation of anything. What
differs is only the data: one approximate intraday path per daily bar.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from src.backtesting.bars import LEGS, calendar_from_bars, day_quotes, previous_closes, sessions
from src.config.limits import RiskLimits, load_risk_limits
from src.decision.engine import Advisor
from src.domain.calendar import NSECalendar
from src.domain.clock import ReplayClock
from src.domain.events import MarkToMarket, TradeClosed
from src.domain.types import Bar, Instrument, KillScope, KillSwitchState
from src.engine.runner import EngineConfig, build_engine
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.store.event_store import EventStore
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

ENVIRONMENT = "backtest"
START = time(8, 50)  # each session starts before PRE_OPEN, like the scheduled paper run


@dataclass(frozen=True)
class BacktestResult:
    store_path: Path
    sessions: tuple[date, ...]
    trades: tuple[TradeClosed, ...]
    equity: Mapping[str, Mapping[date, Decimal]]  # book -> the last mark of each session
    skipped: tuple[date, ...] = ()  # sessions without bars for the universe


@dataclass
class Backtest:
    """One period of sessions over recorded daily bars (raw and adjusted, plus the index)."""

    bars: Sequence[Bar]
    universe: Sequence[Instrument]
    config: EngineConfig = field(
        default_factory=lambda: EngineConfig(environment=ENVIRONMENT, heartbeat=False)
    )
    limits: RiskLimits = field(default_factory=load_risk_limits)
    calendar: NSECalendar | None = None  # default: the sessions the bars traded
    advisors: Mapping[str, Advisor | None] | None = None
    step_s: float = 300.0  # the replay clock's step; path quotes are ~15 minutes apart
    legs: int = LEGS
    # Research only: re-arm halted *strategy* switches before each session, as an operator who
    # reviews and resumes every morning would (they latch otherwise: one losing streak would
    # stop a strategy for the rest of the period). Global halts are never touched.
    rearm_strategies: bool = False

    def __post_init__(self) -> None:
        self._sessions = sessions(self.bars)
        self.trading_calendar = self.calendar or calendar_from_bars(self.bars)

    async def run_session(self, store: EventStore, day: date) -> bool:
        """One paper session on ``day``; False when the bars have nothing for it."""
        today = self._sessions.get(day, [])
        if not today:
            return False
        history = [b for b in self.bars if b.session_date < day]
        quotes = day_quotes(today, previous_closes(self.bars, day), legs=self.legs)
        clock = ReplayClock(datetime.combine(day, START, IST))
        engine = build_engine(
            config=self.config, clock=clock, calendar=self.trading_calendar, store=store,
            quotes=TapeQuoteSource(quotes, clock=clock), history=TapeHistorySource(history),
            universe=self.universe, limits=self.limits, advisors=self.advisors,
        )  # fmt: skip
        if self.rearm_strategies:
            for book in engine.books.values():
                for name, state in book.switches.kill_state().strategies.items():
                    if state is not KillSwitchState.ARMED:
                        book.switches.resume(KillScope.STRATEGY, name=name, actor="backtest",
                                             reason="re-armed for the next session (research)")  # fmt: skip
        run = asyncio.create_task(engine.run())
        while not run.done():
            await clock.advance(self.step_s)
        await run
        return True

    async def run(self, store_path: Path, start: date, end: date) -> BacktestResult:
        days = self.trading_calendar.trading_days(start, end)
        ran, skipped = [], []
        with EventStore(store_path) as store:
            for day in days:
                if await self.run_session(store, day):
                    ran.append(day)
                else:
                    skipped.append(day)
            trades = tuple(e.payload for e in store.read(types=[TradeClosed.event_type])
                           if isinstance(e.payload, TradeClosed))  # fmt: skip
            equity: dict[str, dict[date, Decimal]] = {}
            for e in store.read(types=[MarkToMarket.event_type]):
                if isinstance(e.payload, MarkToMarket):
                    equity.setdefault(e.payload.book_id, {})[e.ist_date] = e.payload.equity
        logger.info("backtest %s..%s: %d sessions, %d trades", start, end, len(ran), len(trades))
        return BacktestResult(store_path=store_path, sessions=tuple(ran), trades=trades,
                              equity=equity, skipped=tuple(skipped))  # fmt: skip
