"""
The risk monitor task (plan M4.3/M4.4): every ``interval_s`` (60 s in month 1), independently of
signal cycles - so a breach on a day with no signals still halts or flattens.

Each tick: re-arm yesterday's daily-loss trips (only if configured), check the ``HALT`` file, mark
the book to market, run :meth:`DailyRiskTracker.risk_tick`, trip the kill switches on any new
breach, and - while a switch is at FLATTEN - take one more :class:`Flattener` step.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from src.domain.clock import Clock
from src.domain.events import Alert
from src.domain.sink import EventSink
from src.oms.position_book import PositionBook
from src.risk.kill_switch import Flattener, KillSwitchRegistry
from src.risk.marks import unrealized_by_strategy
from src.risk.state import Breach, DailyRiskTracker

logger = logging.getLogger(__name__)

MarkSource = Callable[[], Mapping[str, Decimal]]


@dataclass(frozen=True)
class TickReport:
    equity: Decimal
    breaches: tuple[Breach, ...] = ()  # new this tick
    tripped: tuple[Breach, ...] = ()  # those that changed a switch
    halt_file: bool = False
    rearmed: tuple[str, ...] = ()
    flatten_remaining: int = 0


class RiskMonitor:
    def __init__(
        self,
        *,
        book: PositionBook,
        tracker: DailyRiskTracker,
        switches: KillSwitchRegistry,
        flattener: Flattener,
        marks: MarkSource,
        clock: Clock,
        sink: EventSink,
        interval_s: float = 60.0,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.book = book
        self.tracker = tracker
        self.switches = switches
        self.flattener = flattener
        self._marks = marks
        self._clock = clock
        self._sink = sink
        self.interval_s = interval_s
        self._last_marks: dict[str, Decimal] = {}
        self._unmarked: set[str] = set()

    def marks(self) -> dict[str, Decimal]:
        """Latest marks; a held instrument never quoted is valued at cost (and alerted once)."""
        self._last_marks.update(self._marks())
        marks = dict(self._last_marks)
        for position in self.book.positions(self._clock.now()):
            key = position.instrument_key
            if position.quantity and key not in marks:
                marks[key] = position.avg_price or Decimal(0)
                if key not in self._unmarked:
                    self._unmarked.add(key)
                    self._sink.emit(
                        Alert(level="WARNING", key="risk_no_mark",
                              message=f"no quote for held {key}: valued at cost"),
                        source="risk",
                    )  # fmt: skip
        return marks

    def start_day(self) -> None:
        """Seed today's risk state from the current marks (a stored state for today wins)."""
        self.tracker.start_day(self.book.equity(self.marks()))

    async def tick(self) -> TickReport:
        rearmed = tuple(self.switches.new_day())
        halted = self.switches.check_halt_file()
        marks = self.marks()
        equity = self.book.equity(marks)
        breaches = self.tracker.risk_tick(
            equity,
            unrealized_by_strategy=unrealized_by_strategy(self.book, marks, self._clock.now()),
        )
        tripped = self.switches.apply(breaches)
        remaining = 0
        for strategy in self.switches.flattening():
            remaining += await self.flattener.step(marks, strategy)
        return TickReport(equity, tuple(breaches), tuple(tripped), halted, rearmed, remaining)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Tick until ``stop`` is set. A failing tick is logged and alerted; the loop goes on."""
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                logger.exception("risk tick failed")
                self._sink.emit(
                    Alert(level="CRITICAL", key="risk_tick_failed",
                          message=f"{type(exc).__name__}: {exc}"),
                    source="risk",
                )  # fmt: skip
            await self._clock.sleep(self.interval_s)
