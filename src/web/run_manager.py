"""
Run manager — owns the live trading session as a background task and fans state out to
connected WebSocket clients.

Responsibilities:
* Start/stop a single trading run (the v2 engine, :mod:`src.engine.live`; in the ``demo``
  environment the engine on a synthetic day, elsewhere an order-free UI demo generator).
* Hold the web's **own read connection** to the environment's event store (WAL readers never
  block the engine) and the running engine, once a run has built it, for the API's live views.
* Act as the :class:`~src.live.views.SnapshotSink`: cache the latest snapshot + recent cycle
  traces and broadcast every update to subscribers.
* A read-only deployment (``RAKSHAQUANT_WEB_READONLY``) disables run-control entirely.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import get_settings
from src.config.limits import load_risk_limits
from src.domain.calendar import get_calendar
from src.domain.clock import Clock, WallClock
from src.domain.events import ControlCommand
from src.engine.live import STOP_GRACE_S
from src.evaluation.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment
from src.live.recorder import env_badge, snapshot_from_stats
from src.live.views import StreamSessionView
from src.store.event_store import EventStore
from src.web.models import ControlResult
from src.web.queries import LiveView, Queries

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


class RunManager:
    """Owns the background session task and the WebSocket broadcast bus."""

    MAX_CYCLES_KEPT = 200
    QUEUE_MAXSIZE = 2000

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
        self._snapshot: dict[str, Any] | None = None
        self._cycles: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
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
            path = engine.store.path if engine else self._store_path or settings.db_path
            self._reader = EventStore(path)
            demo = settings.environment == "demo"
            self._queries = Queries(
                self._reader, settings=settings,
                experiment=load_experiment(settings.experiment_file or DEFAULT_EXPERIMENT_PATH),
                limits=load_risk_limits(), calendar=get_calendar(),
                clock=engine.clock if engine else self._clock,
                reports_dir=settings.state_dir / "reports" if demo else settings.reports_dir,
                demo=demo, read_only=self._read_only(),
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
        valuations = {}
        for book_id in engine.books:
            try:
                valuations[book_id] = engine.valuation(book_id)
            except KeyError:  # a position without a mark yet (before the first poll)
                continue
        return LiveView(valuations=valuations, marks=engine.market.marks(),
                        tasks=tuple(engine.tasks.running), quote_age_s=ages)  # fmt: skip

    # ── SnapshotSink interface (called from StreamSessionView) ──────────────────────

    def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._broadcast({"type": "snapshot", "data": snapshot})

    def add_cycle(self, cycle: dict[str, Any]) -> None:
        self._cycles.append(cycle)
        if len(self._cycles) > self.MAX_CYCLES_KEPT:
            self._cycles = self._cycles[-self.MAX_CYCLES_KEPT :]
        self._broadcast({"type": "cycle", "data": cycle})

    # ── Broadcast bus ───────────────────────────────────────────────────────────────

    def _broadcast(self, message: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer; drop rather than block
                logger.debug("Dropping WS message for a slow subscriber")

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator of messages for one WebSocket client (init snapshot first)."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._subscribers.add(q)
        q.put_nowait(
            {
                "type": "init",
                "snapshot": self._snapshot,
                "cycles": self._cycles,
                "running": self.is_running,
                "demo": self._demo,
            }
        )
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

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

    # ── Demo generator (no market data / API keys required) ─────────────────────────

    async def _run_demo(self, view: StreamSessionView) -> None:
        """In the demo environment: the real engine on a synthetic, paced day. Elsewhere: a
        fabricated, order-free session so the console is demoable off-market."""
        settings = self.settings
        try:
            if settings.environment == "demo":
                from src.engine.live import run_demo

                assert self._stop_event is not None
                self.close_reader()  # the demo starts from a fresh store file
                await run_demo(settings, view, stop=self._stop_event, on_engine=self.attach)
            else:
                await self._demo_loop(view)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - demo must never crash the server
            logger.exception("Demo session crashed")
            self._broadcast({"type": "error", "data": {"message": "the demo stopped on an error "
                                                                  "(see the logs)"}})  # fmt: skip
        finally:
            self.detach()

    async def _demo_loop(self, view: StreamSessionView) -> None:
        assert self._stop_event is not None
        stats = self._stats
        stats.trading_mode = "paper"
        stats.data_source = "simulated (demo)"
        stats.log_activity("Demo mode — synthetic data, no orders sent", "WARNING")

        symbols = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "ITC", "ICICIBANK", "BHARTIARTL"]
        prices = {s: random.uniform(400, 4200) for s in symbols}
        regimes = [
            ("trending_up", ["momentum", "trend_following"]),
            ("ranging", ["mean_reversion", "breakout"]),
            ("volatile", ["breakout", "momentum"]),
            ("trending_down", ["momentum", "trend_following"]),
        ]
        open_positions: list[dict[str, Any]] = []
        cycle = 0
        view.run_status = "RUNNING"

        while not self._stop_event.is_set():
            cycle += 1
            regime, strategies = random.choice(regimes)
            stats.current_regime = regime
            stats.regime_confidence = round(random.uniform(0.45, 0.9), 2)
            stats.active_strategies = strategies
            stats.cycles_run = cycle
            stats.signals_generated += random.randint(1, 3)

            # Drift prices + build the quotes table.
            for s in symbols:
                prices[s] *= 1 + random.uniform(-0.012, 0.012)
            stats.market_quotes = {
                s: {"last_price": prices[s], "change_percent": random.uniform(-2.5, 2.5)}
                for s in symbols
            }

            # Occasionally open a position.
            if random.random() < 0.5 and len(open_positions) < 5:
                sym = random.choice(symbols)
                side = "BUY" if random.random() < 0.6 else "SELL"
                qty = random.randint(5, 60)
                entry = prices[sym]
                open_positions.append(
                    {"symbol": sym, "side": side, "qty": qty, "entry": entry, "pnl": 0.0}
                )
                stats.signals_validated += 1
                stats.trades_approved += 1
                stats.current_signal = {
                    "signal_type": side,
                    "symbol": sym,
                    "strategy": strategies[0],
                    "confidence": round(random.uniform(0.5, 0.85), 2),
                }
                stats.last_decision_reason = (
                    f"{regime.replace('_', ' ').title()} regime — {strategies[0]} entry on {sym}"
                )
                stats.log_activity(f"TRADE [SHADOW]: {side} {qty} {sym} @ Rs.{entry:,.2f}", "TRADE")
            else:
                stats.signals_rejected += random.randint(0, 1)

            # Mark positions + occasionally close one.
            unrealized = 0.0
            for p in open_positions:
                mark = prices[p["symbol"]]
                pnl = (mark - p["entry"]) * p["qty"] * (1 if p["side"] == "BUY" else -1)
                p["pnl"] = pnl
                unrealized += pnl
            stats.unrealized_pnl = unrealized
            stats.open_positions = list(open_positions)

            if open_positions and random.random() < 0.35:
                closed = open_positions.pop(random.randrange(len(open_positions)))
                pnl = closed["pnl"]
                stats.total_trades += 1
                stats.realized_pnl += pnl
                stats.current_balance += pnl
                stats.unrealized_pnl -= pnl
                if pnl >= 0:
                    stats.winning_trades += 1
                    stats.best_trade = max(stats.best_trade, pnl)
                    stats.log_activity(f"Trade closed: +Rs.{pnl:,.2f}", "SUCCESS")
                else:
                    stats.losing_trades += 1
                    stats.worst_trade = min(stats.worst_trade, pnl)
                    stats.log_activity(f"Trade closed: Rs.{pnl:,.2f}", "ERROR")

            # FinOps + goal.
            stats.llm_calls += random.randint(2, 4)
            in_tok, out_tok = random.randint(600, 1400), random.randint(120, 400)
            stats.llm_tokens += in_tok + out_tok
            stats.llm_cost_usd += (in_tok / 1e6) * 0.59 + (out_tok / 1e6) * 0.79
            stats.goal_enabled = True
            stats.goal_feasible = True
            stats.goal_target_amount = 50000.0
            stats.goal_mtd_pnl = stats.realized_pnl
            stats.goal_expected_to_date = 12000.0
            stats.goal_on_pace = stats.realized_pnl >= 9600.0
            stats.goal_status = "on pace" if stats.goal_on_pace else "behind pace"

            self.set_snapshot(
                snapshot_from_stats(stats, run_status="RUNNING", effective_mode="shadow")
            )
            self.add_cycle(self._demo_cycle(cycle, regime, stats, in_tok, out_tok))

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=random.uniform(2.0, 3.5))
            except TimeoutError:
                pass

    @staticmethod
    def _demo_cycle(
        cycle: int, regime: str, stats: Any, in_tok: int, out_tok: int
    ) -> dict[str, Any]:
        wf = f"DEMO-{datetime.now().strftime('%H%M%S')}-{cycle}"
        conf = stats.regime_confidence

        def span(
            name: str,
            label: str,
            kind: str,
            decision: str,
            reasoning: str,
            it: int = 0,
            ot: int = 0,
            latency: int | None = None,
            conf_v: float | None = None,
        ) -> dict[str, Any]:
            return {
                "id": f"{wf}:{name}",
                "name": name,
                "label": label,
                "kind": kind,
                "decision": decision,
                "confidence": conf_v,
                "reasoning": reasoning,
                "inputTokens": it,
                "outputTokens": ot,
                "tokens": it + ot,
                "costUsd": (it / 1e6) * 0.59 + (ot / 1e6) * 0.79,
                "latencyMs": latency,
                "detail": {"regime": regime},
            }

        r1, r2, r3 = (
            int(in_tok * 0.4),
            int(in_tok * 0.35),
            int(in_tok * 0.25),
        )
        o1, o2, o3 = int(out_tok * 0.4), int(out_tok * 0.35), int(out_tok * 0.25)
        spans = [
            span(
                "support_agents",
                "Support Agents",
                "support",
                "enriched",
                "News / sentiment / prediction enrichment.",
                latency=random.randint(200, 600),
            ),
            span(
                "market_regime",
                "Market Regime",
                "llm",
                regime,
                f"Classified regime {regime} from indicators + enrichment.",
                r1,
                o1,
                random.randint(300, 900),
                conf,
            ),
            span(
                "strategy_selection",
                "Strategy Selection",
                "llm",
                ", ".join(stats.active_strategies),
                "Selected strategies for the regime.",
                r2,
                o2,
                random.randint(250, 700),
            ),
            span(
                "signal_validation",
                "Signal Validation",
                "llm",
                "1 validated / 0 rejected",
                "Filtered raw signals; survivors proceed to risk.",
                r3,
                o3,
                random.randint(250, 700),
            ),
            span(
                "risk_compliance",
                "Risk Compliance",
                "deterministic",
                "1 approved / 0 blocked",
                "Deterministic rules engine: sizing, limits, kill-switch (no LLM).",
                latency=random.randint(5, 30),
            ),
        ]
        return {
            "id": wf,
            "workflowId": wf,
            "ts": datetime.now().isoformat(),
            "status": "success",
            "durationMs": sum(s["latencyMs"] or 0 for s in spans),
            "regime": regime,
            "regimeConfidence": conf,
            "signalsCount": 1,
            "approvedCount": 1,
            "rejectedCount": 0,
            "tokens": in_tok + out_tok,
            "costUsd": (in_tok / 1e6) * 0.59 + (out_tok / 1e6) * 0.79,
            "spans": spans,
        }
