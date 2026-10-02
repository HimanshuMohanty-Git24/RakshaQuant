"""
Run manager — owns the trading session as a background task for the web console.

Responsibilities:
* Start/stop a single trading run of the v2 engine (:mod:`src.engine.live`): paper, or - in
  the ``demo`` environment only - the bundled fixture tape replayed through the same engine.
  Nothing is ever fabricated for the UI.
* Hold the web's **own read connection** to the environment's event store (WAL readers never
  block the engine) and the running engine, once a run has built it, for the API's live views.
* Own the event-stream :class:`~src.web.stream.Hub`.
* A read-only deployment (``RAKSHAQUANT_WEB_READONLY``) disables run-control entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from src.config import get_settings
from src.domain.clock import Clock, ReplayClock, WallClock
from src.domain.events import ControlCommand
from src.engine.live import STOP_GRACE_S, demo_store_path
from src.store.event_store import EventStore
from src.web.models import ControlResult
from src.web.queries import LiveView, Queries, build_queries, live_view
from src.web.stream import Hub

if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.engine.runner import Engine

logger = logging.getLogger(__name__)
T = TypeVar("T")


class RunControlError(RuntimeError):
    """Raised when a run cannot be started (already running, wrong environment, read-only)."""


def _stopped_clock(store: EventStore) -> Clock | None:
    """A clock standing at the store's last event (None for an empty store)."""
    last = store.last_seq()
    found = store.read(since_seq=last - 1, limit=1) if last else []
    return ReplayClock(found[0].ts_utc) if found else None


class RunManager:
    """Owns the background session task, the web's read connection and the stream hub."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store_path: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._store_path = store_path
        self._clock: Clock = clock or WallClock()
        self._reader: EventStore | None = None
        self._queries: Queries | None = None
        self.engine: Engine | None = None
        self._starting: str | None = None  # a start to record once the engine exists
        self.hub = Hub(self)
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # ── The store and the engine ──────────────────────────────────────────────────────

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def queries(self) -> Queries:
        """The read side, on the web's own connection to the active store."""
        if self._queries is None:
            settings = self.settings
            engine = self.engine
            demo = settings.environment == "demo"
            default = demo_store_path(settings) if demo else settings.db_path
            path = engine.store.path if engine else self._store_path or default
            self._reader = EventStore(path)
            clock = engine.clock if engine else self._clock
            if engine is None and demo:  # a finished demo: its clock stopped on the tape's day
                clock = _stopped_clock(self._reader) or clock
            self._queries = build_queries(self._reader, settings, clock=clock,
                                          read_only=self._read_only())  # fmt: skip
        return self._queries

    def attach(self, engine: Engine) -> None:
        """A run built its engine: read its store, on its clock; record who started it."""
        self.engine = engine
        self.close_reader()
        if self._starting is not None:
            engine.sink.emit(ControlCommand(action="session_start", actor="web",
                                            outcome="applied", detail=self._starting),
                             source="web")  # fmt: skip
            self._starting = None

    def detach(self) -> None:
        self.engine = None
        self.close_reader()

    def close_reader(self) -> None:
        if self._reader is not None:
            self._reader.close()
        self._reader, self._queries = None, None

    async def read(self, fn: Callable[[Queries], T]) -> T:
        """Run a store query in a worker thread, on the web's own read connection.

        A session starting or ending swaps that connection on the event loop (``attach`` /
        ``detach``) and closes the old one, possibly under a query still running in its thread.
        Such a read is retried once on the new connection instead of failing the request."""
        queries = self.queries()
        try:
            return await asyncio.to_thread(fn, queries)
        except sqlite3.Error:
            if queries is self._queries:
                raise  # the same connection: a real error
            logger.debug("a read was retried: a session swapped the connection under it")
            return await asyncio.to_thread(fn, self.queries())

    def live_view(self) -> LiveView | None:
        """The engine's in-memory state; call on the event loop (the engine's thread)."""
        return live_view(self.engine) if self.engine is not None else None

    # ── Broadcast (the event stream hub) ──────────────────────────────────────────

    def _broadcast(self, message: dict[str, Any]) -> None:
        """A run notice (``stopped``, ``error``) to the stream's system subscribers."""
        self.hub.announce(str(message["type"]), message.get("data") or {})

    # ── Lifecycle ─────────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @staticmethod
    def _read_only() -> bool:
        """Monitor-only deployment: run-control (start AND stop) is disabled from the UI."""
        return os.getenv("RAKSHAQUANT_WEB_READONLY", "").lower() in ("1", "true", "yes")

    @property
    def read_only(self) -> bool:
        return self._read_only()

    async def start(self, *, demo: bool = False) -> dict[str, Any]:
        """Start a paper run (the v2 engine has no broker path: nothing here can go live)."""
        if self.is_running:
            raise RunControlError("A run is already active.")
        if self._read_only():
            raise RunControlError("Run-control is disabled (RAKSHAQUANT_WEB_READONLY set).")
        in_demo = self.settings.environment == "demo"
        if demo and not in_demo:
            raise RunControlError("The demo runs only in the demo environment: start the console "
                                  "with --demo.")  # fmt: skip
        if in_demo and not demo:
            raise RunControlError("This console runs the demo environment: start a demo run.")
        self._starting = "demo" if demo else "paper"
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(demo), name="rakshaquant-run")
        logger.info("Started a %s run", self._starting)
        return {"running": True, "demo": demo}

    async def stop(self) -> dict[str, Any]:
        # Read-only deployments disable run-control entirely — stopping a run is a control
        # action too, so the browser cannot issue it (the operator stops the server process).
        if self._read_only():
            raise RunControlError("Run-control is disabled (RAKSHAQUANT_WEB_READONLY set).")
        return await self.shutdown()

    async def stop_session(self) -> ControlResult:
        """The operator's stop: recorded, then cooperative (see :meth:`shutdown`)."""
        if self._read_only():
            raise RunControlError("Run-control is disabled (RAKSHAQUANT_WEB_READONLY set).")
        if not self.is_running:
            return ControlResult(action="session_stop", outcome="no_change", books=[],
                                 detail="no run is active")  # fmt: skip
        detail = "stopped cooperatively (the engine finished its current step)"
        if self.engine is not None:
            self.engine.sink.emit(ControlCommand(action="session_stop", actor="web",
                                                 outcome="applied", detail=detail),
                                  source="web")  # fmt: skip
        await self.shutdown()
        return ControlResult(action="session_stop", outcome="applied", books=[], detail=detail)

    async def shutdown(self) -> dict[str, Any]:
        """Stop the run cooperatively (the engine finishes what it is doing; ``_drive`` cancels
        it after its grace period). Not subject to read-only: the server shutdown uses it."""
        if not self.is_running or self._task is None:
            return {"running": False}
        if self._stop_event is not None:
            self._stop_event.set()
        finished, _ = await asyncio.wait({self._task}, timeout=STOP_GRACE_S + 5.0)
        if not finished:  # pragma: no cover - _drive already cancels after its grace
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
            pass
        self._broadcast({"type": "stopped"})
        return {"running": False}

    async def _run(self, demo: bool) -> None:
        """The paper session, or the bundled fixture tape through the real engine (demo
        environment only). The console paints nothing: it reads the store and the engine."""
        from src.engine.live import run_demo, run_paper

        assert self._stop_event is not None
        try:
            if demo:
                self.close_reader()  # the demo starts from a fresh store file
                await run_demo(self.settings, stop=self._stop_event, on_engine=self.attach)
            else:
                await run_paper(self.settings, stop=self._stop_event, on_engine=self.attach)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - surfaced to the UI, never crashes the server
            logger.exception("The %s run crashed", "demo" if demo else "paper")
            self._broadcast({"type": "error", "data": {"message": "the run stopped on an error "
                                                                  "(see the logs)"}})  # fmt: skip
        finally:
            self.detach()
