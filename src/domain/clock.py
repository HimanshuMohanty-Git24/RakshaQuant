"""
Clocks (plan M1.4). Everything time-dependent asks a :class:`Clock`, never ``datetime.now()``,
so a session can run on wall time or on a recorded tape.

* :class:`WallClock` — real time.
* :class:`ReplayClock` — virtual time for replays and tests. Time moves only when a driver calls
  :meth:`ReplayClock.advance_to`; tasks blocked in :meth:`ReplayClock.sleep` are woken in time
  order (FIFO for equal wake times), with the event loop allowed to run between wakes.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from datetime import UTC, datetime, timedelta
from typing import Protocol

from src.utils.market_time import IST


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None: ...

    async def sleep_until(self, ts: datetime) -> None: ...


def now_ist(clock: Clock) -> datetime:
    return clock.now().astimezone(IST)


def _require_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("clock instants must be timezone-aware")
    return ts.astimezone(UTC)


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def sleep_until(self, ts: datetime) -> None:
        await self.sleep((_require_aware(ts) - self.now()).total_seconds())


class ReplayClock:
    """Deterministic virtual time.

    ``settle_rounds`` is how many event-loop turns the clock yields after waking each sleeper,
    so the woken task can reach its next await before time moves on. Tasks under replay should
    await only the clock (no real I/O); raise ``settle_rounds`` if a task needs more turns.
    """

    def __init__(self, start: datetime, *, settle_rounds: int = 10) -> None:
        self._now = _require_aware(start)
        self._settle_rounds = settle_rounds
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def now(self) -> datetime:
        return self._now

    @property
    def pending_sleepers(self) -> int:
        return sum(1 for _, _, fut in self._sleepers if not fut.done())

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        await self.sleep_until(self._now + timedelta(seconds=seconds))

    async def sleep_until(self, ts: datetime) -> None:
        wake = _require_aware(ts)
        if wake <= self._now:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (wake, next(self._order), future))
        await future

    async def advance_to(self, target: datetime) -> None:
        """Move time to ``target``, waking every sleeper due on the way, in order."""
        target = _require_aware(target)
        if target < self._now:
            raise ValueError(f"time cannot go backwards ({target} < {self._now})")
        await self._settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            wake, _, future = heapq.heappop(self._sleepers)
            if future.done():  # the sleeping task was cancelled
                continue
            self._now = wake
            future.set_result(None)
            await self._settle()
        self._now = target

    async def advance(self, seconds: float) -> None:
        await self.advance_to(self._now + timedelta(seconds=seconds))

    async def _settle(self) -> None:
        for _ in range(self._settle_rounds):
            await asyncio.sleep(0)
