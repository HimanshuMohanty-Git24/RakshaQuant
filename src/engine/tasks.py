"""
The engine's periodic tasks (plan M5.6; audit §F.4, §G.3).

Every task is a :func:`periodic` loop on the engine's clock (wall clock live, ReplayClock in
replay): it runs a step, then sleeps its interval, until the stop event is set. A failing step is
logged and alerted (CRITICAL) and the loop carries on - one bad tick never kills a task.

| task         | cadence                 | step                                            |
|--------------|-------------------------|-------------------------------------------------|
| market_data  | the source's delay (60s)| poll quotes → broker fills → exit targets       |
| monitor      | 60 s                    | resolve UNKNOWN orders, risk tick (HALT file,   |
|              |                         | MTM limits, kill switches, flatten)             |
| reconciler   | 15 min                  | OMS vs broker; drift blocks new entries         |
| announcements| the feed's ttl (5 min)  | new corporate announcements (+ classification)  |

The decision cycle and the lifecycle are driven by the session state machine (see
:mod:`src.engine.runner`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

from src.domain.clock import Clock
from src.domain.events import Alert, Heartbeat, LoopLag
from src.domain.sink import EventSink

logger = logging.getLogger(__name__)

MARKET_DATA = "market_data"
MONITOR = "monitor"
RECONCILER = "reconciler"
ANNOUNCEMENTS = "announcements"
MONITOR_INTERVAL_S = 60.0
RECONCILE_INTERVAL_S = 15 * 60.0


async def periodic(
    name: str,
    step: Callable[[], Awaitable[object]],
    *,
    interval_s: Callable[[], float],
    clock: Clock,
    sink: EventSink,
    stop: asyncio.Event,
) -> None:
    """Run ``step`` every ``interval_s()`` seconds until ``stop`` is set."""
    while not stop.is_set():
        try:
            await step()
        except Exception as exc:
            logger.exception("task %s failed", name)
            sink.emit(
                Alert(level="CRITICAL", key=f"task_failed:{name}",
                      message=f"{type(exc).__name__}: {exc}"),
                source="engine",
            )  # fmt: skip
        if stop.is_set():
            break
        await _sleep_unless_stopped(clock, max(1.0, interval_s()), stop)


async def _sleep_unless_stopped(clock: Clock, seconds: float, stop: asyncio.Event) -> None:
    sleeper = asyncio.ensure_future(clock.sleep(seconds))
    stopper = asyncio.ensure_future(stop.wait())
    _, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


HEARTBEAT = "heartbeat"
HEARTBEAT_INTERVAL_S = 60.0
LOOP_LAG_THRESHOLD_MS = 500.0


def heartbeat(sink: EventSink, started: float) -> Callable[[], Awaitable[None]]:
    """A ``Heartbeat`` (pid, uptime, event-loop lag) per run; ``LoopLag`` over 500 ms. The lag is
    the wall time the loop takes to come back to a task that yields - what every task waits."""

    async def step() -> None:
        before = time.perf_counter()
        await asyncio.sleep(0)
        lag_ms = round((time.perf_counter() - before) * 1000, 2)
        uptime = round(time.monotonic() - started, 1)
        sink.emit(Heartbeat(pid=os.getpid(), uptime_s=uptime, loop_lag_ms=lag_ms), source="engine")
        if lag_ms > LOOP_LAG_THRESHOLD_MS:
            sink.emit(LoopLag(lag_ms=lag_ms, threshold_ms=LOOP_LAG_THRESHOLD_MS), source="engine")

    return step


class TaskGroup:
    """The engine's running loops: started when the session opens, stopped at the close."""

    def __init__(self, clock: Clock, sink: EventSink) -> None:
        self._clock = clock
        self._sink = sink
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def running(self) -> list[str]:
        return sorted(n for n, t in self._tasks.items() if not t.done())

    def start(
        self,
        name: str,
        step: Callable[[], Awaitable[object]],
        interval_s: Callable[[], float],
    ) -> None:
        if name in self._tasks and not self._tasks[name].done():
            return
        self._stop.clear()
        self._tasks[name] = asyncio.create_task(
            periodic(name, step, interval_s=interval_s, clock=self._clock, sink=self._sink,
                     stop=self._stop),
            name=f"engine:{name}",
        )  # fmt: skip

    async def stop(self, timeout_s: float = 60.0) -> None:
        """Stop every loop. A loop finishes the step it is in (never abandoning an order
        mid-submit); only a step still running after ``timeout_s`` is cancelled."""
        self._stop.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            _, late = await asyncio.wait(tasks, timeout=timeout_s)
            for task in late:
                logger.error("task %s did not stop in %.0f s: cancelling", task.get_name(),
                             timeout_s)  # fmt: skip
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
