"""
FastAPI server for the RakshaQuant web console.

Every ``/api/*`` route except ``/api/health`` needs the per-launch bearer token, and every
state-changing request must come from the console's own origin (:mod:`src.web.security`).

* ``GET  /api/health``          - liveness only (public, no state).
* ``GET  /api/state``           - latest snapshot (for a cold load).
* ``GET  /api/config``          - a secret-free view of the configuration.
* ``POST /api/run/start``       - start a run ``{demo}``.
* ``POST /api/run/stop``        - stop the active run.
* ``WS   /ws``                  - live stream (token as the ``rq.token.<token>`` subprotocol).
* ``/``                         - the built SPA (``frontend/dist``) when present.

FastAPI / uvicorn are optional deps (the ``web`` extra); this module is only imported when
the app runs in web mode.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from src.config import get_settings
from src.web.run_manager import RunControlError, RunManager, resolve_effective_mode
from src.web.security import (
    DEV_ORIGINS,
    WebSecurity,
    accepted_subprotocol,
    new_token,
    require_same_origin,
    require_token,
    websocket_refusal,
)

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
<p>Then reload this page.</p>
</body></html>"""


class StrictBody(BaseModel):
    """Request bodies: unknown fields and loose types (``"false"`` for a bool) are a 422."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StartRunBody(StrictBody):
    demo: StrictBool = False


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


def _install_error_handlers(app: FastAPI) -> None:
    """Errors never echo exception text or request input back to the client."""

    async def http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HTTPException)
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code,
                            headers=exc.headers)  # fmt: skip

    async def invalid(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return JSONResponse({"error": "invalid request", "fields": fields}, status_code=422)

    async def crashed(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "internal error"}, status_code=500)

    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid)
    app.add_exception_handler(Exception, crashed)


def create_app(
    *,
    manager: RunManager | None = None,
    security: WebSecurity | None = None,
    dev: bool = False,
    auto_start_demo: bool | None = None,
) -> FastAPI:
    """Build the app. ``dev=True`` enables CORS for the Vite dev server; ``auto_start_demo``
    (when not None) starts a run of that kind once the server is up."""
    security = security or WebSecurity.for_launch("127.0.0.1", 8000, token=new_token(), dev=dev)
    run_manager = manager or RunManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if auto_start_demo is not None:
            try:
                await run_manager.start(demo=auto_start_demo)
            except RunControlError as exc:
                logger.warning("Auto-start skipped: %s", exc)
        yield
        await run_manager.shutdown()

    app = FastAPI(title="RakshaQuant Web Console", version="2.0.0", lifespan=lifespan)
    app.state.manager = run_manager
    app.state.security = security
    app.state.websockets = 0
    _install_error_handlers(app)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(security.allowed_hosts))
    if dev:
        app.add_middleware(CORSMiddleware, allow_origins=list(DEV_ORIGINS),
                           allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type"])  # fmt: skip

    def mgr() -> RunManager:
        return cast(RunManager, app.state.manager)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    api = APIRouter(prefix="/api", dependencies=[Depends(require_same_origin),
                                                 Depends(require_token)])  # fmt: skip

    @api.get("/state")
    async def state() -> dict[str, Any]:
        return mgr().state()

    @api.get("/config")
    async def config() -> dict[str, Any]:
        return _safe_config()

    @api.post("/run/start")
    async def run_start(body: StartRunBody | None = None) -> JSONResponse:
        try:
            result = await mgr().start(demo=(body or StartRunBody()).demo)
            return JSONResponse(result)
        except RunControlError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @api.post("/run/stop")
    async def run_stop() -> JSONResponse:
        try:
            return JSONResponse(await mgr().stop())
        except RunControlError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    app.include_router(api)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        refusal = websocket_refusal(websocket, app.state.websockets)
        if refusal is not None:  # accept only to deliver the close code; nothing is sent
            await websocket.accept()
            await websocket.close(code=refusal)
            return
        await websocket.accept(subprotocol=accepted_subprotocol(websocket))
        app.state.websockets += 1
        try:
            async for message in mgr().subscribe():
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - client vanished mid-send
            logger.debug("WebSocket closed: %s", type(exc).__name__)
        finally:
            app.state.websockets -= 1

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
    allow_remote: bool = False,
) -> None:
    """Launch the web console with uvicorn. Blocks until interrupted. The launch URL (with the
    token) is printed once to the console and never logged."""
    import uvicorn

    security = WebSecurity.for_launch(host, port, dev=dev, allow_remote=allow_remote)
    app = create_app(manager=RunManager(), security=security, dev=dev,
                     auto_start_demo=demo if auto_start else None)  # fmt: skip
    logger.info("RakshaQuant web console on %s:%d (demo=%s)", host, port, demo)
    print(f"\n  RakshaQuant web console -> {security.url(host, port)}\n"
          "  (the link carries this launch's access token; it changes on every start)\n")  # fmt: skip
    uvicorn.run(app, host=host, port=port, log_level="warning")
