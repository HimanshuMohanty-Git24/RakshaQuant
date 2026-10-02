"""
FastAPI server for the RakshaQuant web console.

Every ``/api/*`` route except ``/api/health`` needs the per-launch bearer token, and every
state-changing request must come from the console's own origin (:mod:`src.web.security`).

* ``GET  /api/health``          - liveness only (public, no state).
* ``GET  /api/state``           - the legacy console snapshot (until M10).
* ``GET  /api/summary|positions|orders|fills|trades`` - the books and the blotter.
* ``GET  /api/decisions`` (filters) and ``/api/decisions/{id}`` - a decision's full lineage.
* ``GET  /api/risk|books`` - limits, kill switches, rejections; the paired-book comparison.
* ``GET  /api/ai/calls|spend|models|decision-models`` - AI calls, spend, model health.
* ``GET  /api/market/{symbol}/bars``, ``/api/events/typed``, ``/api/reports/{date}``.
* ``GET  /api/system``, ``/api/config`` - process health; read-only redacted configuration.
* ``POST /api/run/start``       - start a run ``{demo}``.
* ``POST /api/run/stop``        - stop the active run.
* ``WS   /ws``                  - live stream (token as the ``rq.token.<token>`` subprotocol).
* ``/``                         - the built SPA (``frontend/dist``) when present.

FastAPI / uvicorn are optional deps (the ``web`` extra); this module is only imported when
the app runs in web mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path as FilePath
from typing import Annotated, Any, Literal, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from src.domain.events import Disposition
from src.engine.market import INDEX_KEY
from src.web.models import BarRow as BarRowModel
from src.web.models import (
    Bars,
    BooksView,
    ConfigView,
    DecisionModelStats,
    DecisionRow,
    FillRow,
    Lineage,
    LLMCallRow,
    OrderRow,
    PositionRow,
    RiskView,
    RoleModels,
    SpendView,
    Summary,
    SystemView,
    TradeRow,
    TypedEventRow,
)
from src.web.queries import MAX_ROWS, GroupBy, Queries
from src.web.run_manager import RunControlError, RunManager
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

_FRONTEND_DIST = FilePath(__file__).resolve().parents[2] / "frontend" / "dist"

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


T = TypeVar("T")

# Query-parameter shapes (anything else is a 422 before it reaches a query).
Book = Annotated[str | None, Query(pattern=r"^[A-Za-z0-9]{1,16}$")]
Symbol = Annotated[str | None, Query(pattern=r"^[A-Z0-9&_.^-]{1,32}$")]
Strategy = Annotated[str | None, Query(pattern=r"^[a-z_]{1,32}$")]
Day = Annotated[date | None, Query(alias="date")]
Limit = Annotated[int, Query(ge=1, le=MAX_ROWS)]
SYMBOL_PATH = r"^[A-Z0-9&_.^-]{1,32}$"
DECISION_ID = r"^[A-Za-z0-9_-]{1,64}$"


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

    async def read(fn: Callable[[Queries], T]) -> T:
        """Run a store query in a worker thread on the web's own read connection."""
        return await asyncio.to_thread(fn, mgr().queries())

    @api.get("/state")
    async def state() -> dict[str, Any]:
        return mgr().state()

    @api.get("/summary")
    async def summary() -> Summary:
        live, running = mgr().live_view(), mgr().is_running
        return await read(lambda q: q.summary(live, running=running))

    @api.get("/positions")
    async def positions(book: Book = None) -> list[PositionRow]:
        live = mgr().live_view()
        return await read(lambda q: q.positions(live, book=book))

    @api.get("/orders")
    async def orders(
        book: Book = None,
        status: Annotated[str | None, Query(pattern=r"^[A-Z_]{1,24}$")] = None,
        day: Day = None,
        limit: Limit = 200,
    ) -> list[OrderRow]:
        return await read(lambda q: q.orders(book=book, status=status, day=day, limit=limit))

    @api.get("/fills")
    async def fills(book: Book = None, day: Day = None, limit: Limit = 200) -> list[FillRow]:
        return await read(lambda q: q.fills(book=book, day=day, limit=limit))

    @api.get("/trades")
    async def trades(
        book: Book = None, day: Day = None, strategy: Strategy = None, limit: Limit = 200
    ) -> list[TradeRow]:
        return await read(lambda q: q.trades(book=book, day=day, strategy=strategy, limit=limit))

    @api.get("/decisions")
    async def decisions(
        book: Book = None,
        symbol: Symbol = None,
        strategy: Strategy = None,
        outcome: Disposition | None = None,
        day: Day = None,
        limit: Limit = 200,
    ) -> list[DecisionRow]:
        wanted = outcome.value if outcome is not None else None
        return await read(lambda q: q.decisions(book=book, symbol=symbol, strategy=strategy,
                                                outcome=wanted, day=day, limit=limit))  # fmt: skip

    @api.get("/decisions/{decision_id}")
    async def decision(decision_id: Annotated[str, Path(pattern=DECISION_ID)]) -> Lineage:
        found = await read(lambda q: q.lineage(decision_id))
        if found is None:
            raise HTTPException(status_code=404, detail="not found")
        return found

    @api.get("/risk")
    async def risk(book: Book = None) -> RiskView:
        return await read(lambda q: q.risk(book=book))

    @api.get("/books")
    async def books() -> BooksView:
        live = mgr().live_view()
        return await read(lambda q: q.books_view(live))

    @api.get("/ai/calls")
    async def ai_calls(
        role: Strategy = None, book: Book = None, day: Day = None, limit: Limit = 200
    ) -> list[LLMCallRow]:
        return await read(lambda q: q.llm_calls(role=role, book=book, day=day, limit=limit))

    @api.get("/ai/spend")
    async def ai_spend(
        group_by: GroupBy = "role", since: date | None = None, until: date | None = None
    ) -> SpendView:
        return await read(lambda q: q.spend(group_by=group_by, since=since, until=until))

    @api.get("/ai/models")
    async def ai_models() -> list[RoleModels]:
        return await read(lambda q: q.models())

    @api.get("/ai/decision-models")
    async def ai_decision_models(day: Day = None) -> list[DecisionModelStats]:
        return await read(lambda q: q.decision_models(day=day))

    @api.get("/market/{symbol}/bars")
    async def bars(
        symbol: Annotated[str, Path(pattern=SYMBOL_PATH)],
        days: Annotated[int, Query(ge=1, le=2000)] = 250,
        adjusted: bool = True,
    ) -> Bars:
        engine = mgr().engine
        key = INDEX_KEY if symbol in ("NIFTY", "NIFTY50", "^NSEI") else None
        if engine is not None and key is None:
            key = next((k for k, i in engine.market.instruments.items() if i.symbol == symbol),
                       None)  # fmt: skip
        key = key or f"NSE:EQ:{symbol}"
        series = engine.market.daily(key) if engine is not None else None
        source: Literal["engine", "tape", "none"]
        if series is not None:
            found, source = series.bars(adjusted=adjusted), "engine"
        else:
            found = await read(lambda q: q.tape_bars(key, adjusted=adjusted))
            source = "tape" if found else "none"
        rows = [BarRowModel(date=b.session_date, open=b.open, high=b.high, low=b.low,
                            close=b.close, volume=b.volume) for b in found[-days:]]  # fmt: skip
        return Bars(symbol=symbol, instrument_key=key, adjusted=adjusted, source=source, bars=rows)

    @api.get("/events/typed")
    async def typed_events(
        symbol: Symbol = None, day: Day = None, limit: Limit = 200
    ) -> list[TypedEventRow]:
        return await read(lambda q: q.typed_events(symbol=symbol, day=day, limit=limit))

    @api.get("/reports/{day}")
    async def report(day: date) -> dict[str, Any]:
        found = await read(lambda q: q.report(day))
        if found is None:
            raise HTTPException(status_code=404, detail="not found")
        return found

    @api.get("/system")
    async def system() -> SystemView:
        live, running = mgr().live_view(), mgr().is_running
        return await read(lambda q: q.system(live, running=running))

    @api.get("/config")
    async def config() -> ConfigView:
        return await read(lambda q: q.config())

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
