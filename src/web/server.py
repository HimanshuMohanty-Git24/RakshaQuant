"""
FastAPI server for the RakshaQuant web console.

Exposes:
* ``GET  /api/health``          — liveness + whether a run is active.
* ``GET  /api/state``           — latest snapshot + recent cycle traces (for a cold load).
* ``GET  /api/cycles``          — recent cycle traces.
* ``GET  /api/cycles/{id}``     — one cycle trace (drawer / deep-link).
* ``GET  /api/config``          — safe, secret-free view of the active configuration.
* ``POST /api/run/start``       — start a run  ({demo, confirmLive}); guarded for LIVE.
* ``POST /api/run/stop``        — stop the active run.
* ``WS   /ws``                  — live snapshot/cycle stream.
* ``/``                         — the built SPA (``frontend/dist``) when present.

FastAPI / uvicorn are optional deps (the ``web`` extra); this module is only imported when
the app runs in web mode.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config import get_settings
from src.web.run_manager import RunControlError, RunManager, resolve_effective_mode

logger = logging.getLogger(__name__)

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

_PLACEHOLDER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>RakshaQuant Web Console</title>
<style>body{background:#0B0C0E;color:#E7E9EC;font-family:ui-monospace,Menlo,monospace;
padding:3rem;line-height:1.6}code{color:#F5A623}a{color:#5B8DEF}</style></head>
<body><h1>RakshaQuant Web Console</h1>
<p>The API is running, but the frontend has not been built yet.</p>
<p>Build it once with:</p>
<pre><code>cd frontend
npm install
npm run build</code></pre>
<p>Then reload this page. The API is live at <a href="/api/health">/api/health</a>.</p>
</body></html>"""


def _safe_config() -> dict[str, Any]:
    """A secret-free projection of settings for the UI (never expose SecretStr values)."""
    s = get_settings()
    effective = resolve_effective_mode(s)
    return {
        "tradingMode": getattr(s, "trading_mode", "paper"),
        "executionMode": getattr(s, "execution_mode", "local_paper"),
        "effectiveMode": effective,
        "env": {"live": "LIVE", "shadow": "SHADOW", "dhan_paper": "SHADOW"}.get(effective, "PAPER"),
        "marketDataSource": getattr(s, "market_data_source", "yfinance"),
        "allowLiveOrders": bool(getattr(s, "allow_live_orders", False)),
        "enableNewsAnalysis": bool(getattr(s, "enable_news_analysis", False)),
        "enableLearning": bool(getattr(s, "enable_learning", False)),
        "riskPerTrade": float(getattr(s, "risk_per_trade", 0.0) or 0.0),
        "maxDailyTrades": int(getattr(s, "max_daily_trades", 0) or 0),
        "dailyLossLimit": float(getattr(s, "daily_loss_limit", 0.0) or 0.0),
        "paperWalletBalance": float(getattr(s, "paper_wallet_balance", 0.0) or 0.0),
        "dailyTokenBudget": int(getattr(s, "daily_token_budget", 0) or 0),
        "dailyCostBudgetUsd": float(getattr(s, "daily_cost_budget_usd", 0.0) or 0.0),
    }


def create_app(*, manager: RunManager | None = None, dev: bool = False) -> FastAPI:
    """Build the FastAPI app. ``dev=True`` enables CORS for the Vite dev server."""
    app = FastAPI(title="RakshaQuant Web Console", version="1.0.0")
    app.state.manager = manager or RunManager()

    if dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def mgr() -> RunManager:
        return cast(RunManager, app.state.manager)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "running": mgr().is_running}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return mgr().state()

    @app.get("/api/cycles")
    async def cycles() -> dict[str, Any]:
        return {"cycles": mgr().state()["cycles"]}

    @app.get("/api/cycles/{cycle_id}")
    async def cycle(cycle_id: str) -> JSONResponse:
        found = mgr().cycle(cycle_id)
        if found is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(found)

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return _safe_config()

    @app.post("/api/run/start")
    async def run_start(body: dict[str, Any] | None = None) -> JSONResponse:
        body = body or {}
        try:
            result = await mgr().start(
                demo=bool(body.get("demo", False)),
                confirm_live=bool(body.get("confirmLive", False)),
            )
            return JSONResponse(result)
        except RunControlError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/run/stop")
    async def run_stop() -> JSONResponse:
        try:
            return JSONResponse(await mgr().stop())
        except RunControlError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async for message in mgr().subscribe():
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - client vanished mid-send
            logger.debug("WebSocket closed: %s", exc)

    # Serve the built SPA (if present); otherwise a helpful placeholder.
    if _FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="spa")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return _PLACEHOLDER_HTML

    return app


def run_web(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    demo: bool = False,
    dev: bool = False,
    auto_start: bool = True,
) -> None:
    """Launch the web console with uvicorn. Blocks until interrupted."""
    import uvicorn

    manager = RunManager()
    app = create_app(manager=manager, dev=dev)

    if auto_start:

        @app.on_event("startup")
        async def _auto_start() -> None:
            try:
                await manager.start(demo=demo)
            except RunControlError as exc:
                logger.warning("Auto-start skipped: %s", exc)

    banner = f"http://{host}:{port}"
    logger.info("RakshaQuant web console -> %s  (demo=%s)", banner, demo)
    print(f"\n  RakshaQuant web console -> {banner}   (demo={demo})\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
