"""
Run manager — owns the live trading session as a background task and fans state out to
connected WebSocket clients.

Responsibilities:
* Start/stop a single trading run of the v2 engine (:mod:`src.engine.live`): paper, or - in
  the ``demo`` environment only - the bundled fixture tape replayed through the same engine.
  Nothing is ever fabricated for the UI.
* Hold the web's **own read connection** to the environment's event store (WAL readers never
  block the engine) and the running engine, once a run has built it, for the API's live views.
* Own the event-stream :class:`~src.web.stream.Hub` and act as the legacy console's
  :class:`~src.live.views.SnapshotSink` (its snapshot is the stream's ``console`` slot).
* A read-only deployment (``RAKSHAQUANT_WEB_READONLY``) disables run-control entirely.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import get_settings
from src.config.limits import load_risk_limits
from src.domain.calendar import get_calendar
from src.domain.clock import Clock, ReplayClock, WallClock
from src.domain.events import ControlCommand
from src.engine.demo import DEMO_TAPE
from src.engine.live import STOP_GRACE_S, demo_store_path
from src.evaluation.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment
from src.live.recorder import env_badge
from src.live.views import StreamSessionView
from src.store.event_store import EventStore
from src.web.models import ControlResult
from src.web.queries import LiveView, Queries
from src.web.stream import Hub

if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.engine.runner import Engine

logger = logging.getLogger(__name__)


class RunControlError(RuntimeError):
    """Raised when a run cannot be started (already running, live-unconfirmed, read-only)."""


def resolve_effective_mode(settings: Any) -> str:
    """
    Best-effort resolution of the effective execution mode for the *pre-run* badge.

    Mirrors ``ExecutionService``: a ``live``/``dhan_paper`` request without the master
    ``allow_live_orders`` gate resolves to ``shadow``. The authoritative value is set on the
    view once the session builds the real ExecutionService.
    """
    mode = str(getattr(settings, "execution_mode", "local_paper"))
    if mode in ("live", "dhan_paper") and not bool(getattr(settings, "allow_live_orders", False)):
        return "shadow"
    return mode


def _stopped_clock(store: EventStore) -> Clock | None:
    """A clock standing at the store's last event (None for an empty store)."""
    last = store.last_seq()
    found = store.read(since_seq=last - 1, limit=1) if last else []
    return ReplayClock(found[0].ts_utc) if found else None


class RunManager:
    """Owns the background session task and the WebSocket broadcast bus."""

    MAX_CYCLES_KEPT = 200

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
        self._snapshot: dict[str, Any] | None = None
        self._cycles: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._stats: Any = None
        self._demo = False

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
            self._queries = Queries(
                self._reader, settings=settings,
                experiment=load_experiment(settings.experiment_file or DEFAULT_EXPERIMENT_PATH),
                limits=load_risk_limits(), calendar=get_calendar(),
                clock=clock,
                reports_dir=settings.state_dir / "reports" if demo else settings.reports_dir,
                demo=demo, read_only=self._read_only(),
                tape_dir=DEMO_TAPE if demo else settings.tape_dir,  # what the demo replays
            )  # fmt: skip
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

    def live_view(self) -> LiveView | None:
        """The engine's in-memory state; call on the event loop (the engine's thread)."""
        engine = self.engine
        if engine is None:
            return None
        now = engine.clock.now()
        ages: dict[str, float] = {}
        for quote in engine.market.quotes().values():
            source = quote.source.value
            ages[source] = round(min(ages.get(source, math.inf), quote.age_seconds(now)), 1)
        valuations, risk = {}, {}
        for book_id, book in engine.books.items():
            try:
                valuations[book_id] = engine.valuation(book_id)
            except KeyError:  # a position without a mark yet (before the first poll)
                pass
            try:
                risk[book_id] = book.gate.book_snapshot()
            except Exception:  # a view must never fail on the engine's account
                logger.exception("risk snapshot for book %s failed", book_id)
        return LiveView(valuations=valuations, marks=engine.market.marks(),
                        tasks=tuple(engine.tasks.running), quote_age_s=ages, risk=risk,
                        managed={b: book.exits.positions for b, book in engine.books.items()},
                        instruments=dict(engine.market.instruments),
                        quotes=engine.market.quotes(), index_closes=engine.index_closes())  # fmt: skip

    # ── SnapshotSink interface (called from StreamSessionView) ──────────────────────

    def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self.hub.publish("console", snapshot)  # the legacy console's snapshot (until M10)

    def add_cycle(self, cycle: dict[str, Any]) -> None:
        self._cycles.append(cycle)
        if len(self._cycles) > self.MAX_CYCLES_KEPT:
            self._cycles = self._cycles[-self.MAX_CYCLES_KEPT :]

    # ── Broadcast (the event stream hub) ──────────────────────────────────────────

    def _broadcast(self, message: dict[str, Any]) -> None:
        """A run notice (``stopped``, ``error``) to the stream's system subscribers."""
        self.hub.announce(str(message["type"]), message.get("data") or {})

    # ── State accessors ─────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> dict[str, Any]:
        return {
            "snapshot": self._snapshot,
            "cycles": self._cycles,
            "running": self.is_running,
            "demo": self._demo,
        }

    def cycle(self, cycle_id: str) -> dict[str, Any] | None:
        return next((c for c in self._cycles if c.get("id") == cycle_id), None)

    # ── Lifecycle ─────────────────────────────────────────────────────────────────

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

        effective = resolve_effective_mode(self.settings)

        from src.dashboard.cli import TradingStats

        self._stats = TradingStats()
        self._cycles = []
        self._demo = demo
        self._starting = "demo" if demo else "paper"
        self._stop_event = asyncio.Event()
        view = StreamSessionView(self._stats, self, effective_mode=effective)

        coro = self._run_demo(view) if demo else self._run_real(view)
        self._task = asyncio.create_task(coro, name="rakshaquant-run")
        logger.info("Started %s run (effective mode=%s)", "demo" if demo else "live", effective)
        return {"running": True, "demo": demo, "env": env_badge(effective)}

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

    async def _run_real(self, view: StreamSessionView) -> None:
        assert self._stop_event is not None
        from src.engine.live import run_paper

        try:
            await run_paper(self.settings, view, stop=self._stop_event, on_engine=self.attach)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - surfaced to the UI, never crashes server
            logger.exception("Trading session crashed")
            self._broadcast({"type": "error", "data": {"message": "the run stopped on an error "
                                                                  "(see the logs)"}})  # fmt: skip
        finally:
            self.detach()

    async def _run_demo(self, view: StreamSessionView) -> None:
        """The bundled fixture tape through the real engine (demo environment only)."""
        from src.engine.live import run_demo

        assert self._stop_event is not None
        try:
            self.close_reader()  # the demo starts from a fresh store file
            await run_demo(self.settings, view, stop=self._stop_event, on_engine=self.attach)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - demo must never crash the server
            logger.exception("Demo session crashed")
            self._broadcast({"type": "error", "data": {"message": "the demo stopped on an error "
                                                                  "(see the logs)"}})  # fmt: skip
        finally:
            self.detach()
