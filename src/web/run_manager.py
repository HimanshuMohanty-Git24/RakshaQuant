"""
Run manager — owns the live trading session as a background task and fans state out to
connected WebSocket clients.

Responsibilities:
* Start/stop a single trading run (the real :func:`run_trading_session`, or a self-contained
  ``demo`` generator so the console is fully usable off-market / without API keys).
* Act as the :class:`~src.live.views.SnapshotSink`: cache the latest snapshot + recent cycle
  traces and broadcast every update to subscribers.
* Enforce run-control safety: a run resolving to the **LIVE** environment is refused unless
  the caller explicitly confirms, and a read-only deployment can disable run-control entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from src.config import get_settings
from src.live.recorder import env_badge, snapshot_from_stats
from src.live.session import run_trading_session
from src.live.views import StreamSessionView

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

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] | None = None
        self._cycles: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._stats: Any = None
        self._demo = False

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

    async def start(self, *, demo: bool = False, confirm_live: bool = False) -> dict[str, Any]:
        if self.is_running:
            raise RunControlError("A run is already active.")
        if os.getenv("RAKSHAQUANT_WEB_READONLY", "").lower() in ("1", "true", "yes"):
            raise RunControlError("Run-control is disabled (RAKSHAQUANT_WEB_READONLY set).")

        settings = get_settings()
        effective = resolve_effective_mode(settings)

        if not demo and effective == "live" and not confirm_live:
            raise RunControlError(
                "Starting a LIVE run requires explicit confirmation (confirmLive=true)."
            )

        from src.dashboard.cli import TradingStats

        self._stats = TradingStats()
        self._cycles = []
        self._demo = demo
        self._stop_event = asyncio.Event()
        view = StreamSessionView(self._stats, self, effective_mode=effective)

        coro = self._run_demo(view) if demo else self._run_real(view)
        self._task = asyncio.create_task(coro, name="rakshaquant-run")
        logger.info("Started %s run (effective mode=%s)", "demo" if demo else "live", effective)
        return {"running": True, "demo": demo, "env": env_badge(effective)}

    async def stop(self) -> dict[str, Any]:
        if not self.is_running or self._task is None:
            return {"running": False}
        if self._stop_event is not None:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
            pass
        self._broadcast({"type": "stopped"})
        return {"running": False}

    async def _run_real(self, view: StreamSessionView) -> None:
        assert self._stop_event is not None
        try:
            await run_trading_session(view, stop_event=self._stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - surfaced to the UI, never crashes server
            logger.exception("Trading session crashed")
            self._broadcast({"type": "error", "data": {"message": str(exc)}})

    # ── Demo generator (no market data / API keys required) ─────────────────────────

    async def _run_demo(self, view: StreamSessionView) -> None:
        """Fabricate a realistic-looking session so the console is demoable off-market."""
        try:
            await self._demo_loop(view)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - demo must never crash the server
            logger.exception("Demo session crashed")
            self._broadcast({"type": "error", "data": {"message": str(exc)}})

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
            span("support_agents", "Support Agents", "support", "enriched",
                 "News / sentiment / prediction enrichment.", latency=random.randint(200, 600)),
            span("market_regime", "Market Regime", "llm", regime,
                 f"Classified regime {regime} from indicators + enrichment.",
                 r1, o1, random.randint(300, 900), conf),
            span("strategy_selection", "Strategy Selection", "llm",
                 ", ".join(stats.active_strategies),
                 "Selected strategies for the regime.", r2, o2, random.randint(250, 700)),
            span("signal_validation", "Signal Validation", "llm", "1 validated / 0 rejected",
                 "Filtered raw signals; survivors proceed to risk.",
                 r3, o3, random.randint(250, 700)),
            span("risk_compliance", "Risk Compliance", "deterministic", "1 approved / 0 blocked",
                 "Deterministic rules engine: sizing, limits, kill-switch (no LLM).",
                 latency=random.randint(5, 30)),
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
