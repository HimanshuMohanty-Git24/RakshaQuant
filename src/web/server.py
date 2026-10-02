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
* ``POST /api/session/start|stop`` - start a paper (or demo) run; stop it cooperatively.
* ``POST /api/risk/halt|resume|flatten`` - the books' kill switches (resume and flatten need
  ``confirm: true`` and the typed phrase; halt works even in read-only mode).
* ``WS   /ws``                  - the event stream: replay from ``since_seq``, then tail
  (token as the ``rq.token.<token>`` subprotocol).
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
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from src.domain.events import Disposition
from src.domain.types import KillScope
from src.engine.market import INDEX_KEY
from src.web.control import ControlRefusedError, Controls
from src.web.models import BarRow as BarRowModel
from src.web.models import (
    Bars,
    BooksView,
    ConfigView,
    ControlResult,
    DecisionModelStats,
    DecisionRow,
    ErrorBody,
    FillRow,
    FlattenBody,
    HaltBody,
    Lineage,
    LLMCallRow,
    OrderRow,
    PositionRow,
    ResumeBody,
    RiskView,
    RoleModels,
    SessionStartBody,
    SessionStopBody,
    SpendView,
    StreamEnvelope,
    StreamSubscribe,
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
from src.web.stream import serve

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
    frontend_dist: FilePath | None = None,
) -> FastAPI:
    """Build the app. ``dev=True`` enables CORS for the Vite dev server; ``auto_start_demo``
    (when not None) starts a run of that kind once the server is up; ``frontend_dist`` is the
    built SPA (default ``frontend/dist``)."""
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
        await run_manager.hub.stop()

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

    errors: dict[int | str, dict[str, Any]] = {
        code: {"model": ErrorBody, "description": text}
        for code, text in ((401, "Missing or wrong token"), (403, "Forbidden"),
                           (404, "Not found"), (409, "Conflict"), (422, "Invalid request"))
    }  # fmt: skip
    api = APIRouter(prefix="/api", responses=errors,
                    dependencies=[Depends(require_same_origin), Depends(require_token)])  # fmt: skip

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

    def refuse_if_read_only() -> None:
        if mgr().read_only:
            raise HTTPException(status_code=403, detail="read-only: run control is disabled")

    def control(action: Callable[[Controls], ControlResult]) -> ControlResult:
        try:
            return action(Controls(mgr()))
        except ControlRefusedError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from None

    @api.post("/session/start")
    async def session_start(body: SessionStartBody | None = None) -> ControlResult:
        refuse_if_read_only()
        demo = (body or SessionStartBody()).demo
        try:
            await mgr().start(demo=demo)
        except RunControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return ControlResult(action="session_start", outcome="applied", books=[],
                             detail="demo" if demo else "paper")  # fmt: skip

    @api.post("/session/stop")
    async def session_stop(body: SessionStopBody | None = None) -> ControlResult:
        """Cooperative: the engine finishes the step it is in; cancelled only after 30 s."""
        refuse_if_read_only()
        return await mgr().stop_session()

    @api.post("/risk/halt")
    async def risk_halt(body: HaltBody) -> ControlResult:
        """Block new entries. Allowed even in read-only mode (stopping risk is always OK)."""
        return control(lambda c: c.halt(book=body.book, reason=body.reason))

    @api.post("/risk/resume")
    async def risk_resume(body: ResumeBody) -> ControlResult:
        refuse_if_read_only()
        return control(lambda c: c.resume(book=body.book, reason=body.reason,
                                          scope=KillScope(body.scope), name=body.name))  # fmt: skip

    @api.post("/risk/flatten")
    async def risk_flatten(body: FlattenBody) -> ControlResult:
        refuse_if_read_only()
        return control(lambda c: c.flatten(book=body.book, reason=body.reason))

    app.include_router(api)

    def openapi() -> dict[str, Any]:
        """The contract (plan M9.5), plus the stream's message shapes as components."""
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            for model in (StreamSubscribe, StreamEnvelope):
                components[model.__name__] = model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        """The event stream (see :mod:`src.web.stream`): send ``{"subscribe": [topics],
        "since_seq": n}``; stored events after ``n`` are replayed, then new ones follow."""
        refusal = websocket_refusal(websocket, app.state.websockets)
        if refusal is not None:  # accept only to deliver the close code; nothing is sent
            await websocket.accept()
            await websocket.close(code=refusal)
            return
        await websocket.accept(subprotocol=accepted_subprotocol(websocket))
        app.state.websockets += 1
        hub = mgr().hub
        sub = hub.connect()
        try:
            await serve(hub, sub, websocket)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - client vanished mid-send
            logger.debug("WebSocket closed: %s", type(exc).__name__)
        finally:
            hub.disconnect(sub)
            app.state.websockets -= 1

    # Serve the built SPA (if present); otherwise a helpful placeholder.
    dist = (frontend_dist or _FRONTEND_DIST).resolve()
    if (dist / "index.html").is_file():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            """A file at the root of the build, else ``index.html``: the client routes deep
            links such as ``/decisions/<id>`` (an unknown ``/api`` path stays a 404)."""
            if path.startswith(("api/", "ws")) or path == "api":
                raise HTTPException(status_code=404, detail="not found")
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
    else:

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
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
          "  (the link carries this launch's access token; it changes on every start)\n",
          flush=True)  # fmt: skip
    uvicorn.run(app, host=host, port=port, log_level="warning")
