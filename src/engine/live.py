"""
Running the v2 engine behind a front end (plan M5.6): the CLI dashboard or the web console, both
through the existing :class:`~src.live.views.SessionView`, fed from the store's projections.

* :func:`run_paper` - today's session on the wall clock with YFinance data (taped for replay),
  the pinned NIFTY 50 universe and the simulated broker, for every book of the experiment
  (``src/config/experiment.yaml``: A deterministic, B typed veto, C LLM veto). **Paper only**:
  the v2 engine has no live broker path; a live ``EXECUTION_MODE`` is ignored with a warning.
* :func:`run_demo` - the same engine on a synthetic, paced day in the ``demo`` environment
  (advisors abstain: no decision model or LLM is called in the demo).

The CLI/web views show the primary book (A); the paired comparison is the daily report's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from pathlib import Path

from src.config.errors import ConfigError
from src.config.limits import load_risk_limits
from src.config.settings import Settings
from src.decision_models.cascade import Cascade
from src.decision_models.setup import build_cascade
from src.decision_models.tasks.announcements import (
    AnnouncementPipeline,
    EventClassifier,
    unclassified,
)
from src.domain.calendar import get_calendar
from src.domain.clock import Clock, ReplayClock, WallClock, now_ist
from src.domain.events import Alert, AnnouncementReceived
from src.domain.types import Instrument
from src.engine.demo import demo_day, demo_instruments, pace, synthetic_day
from src.engine.runner import Engine, build_engine, held_instruments
from src.engine.view_model import StatsProjector
from src.evaluation.books import build_advisors, engine_config
from src.evaluation.daily_report import (
    ReportInputs,
    build_report,
    send_summary,
    telegram_summary,
    write_report,
)
from src.evaluation.experiment import DEFAULT_EXPERIMENT_PATH, ExperimentConfig, load_experiment
from src.evaluation.review import review_day
from src.live.views import SessionView
from src.llm.registry import validate_roles
from src.llm.router import LLMRouter, StoreResponseCache
from src.llm.setup import build_router
from src.marketdata.announcements import AnnouncementIngestor, watermark
from src.marketdata.history import YFinanceHistorySource
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.marketdata.validation import QuoteValidator, band_lookup
from src.marketdata.yfinance_source import YFinanceQuoteSource
from src.notifications.telegram import TelegramNotifier
from src.reference.refresh import alert_reference, refresh_reference
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.store.tape import TapeWriter
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

PAPER_MODES = frozenset({"local_paper", "shadow"})
DM_CACHE = "dm_cache"  # decision-model answers, by state and questions
DEMO = "demo"


async def run_paper(
    settings: Settings,
    view: SessionView,
    *,
    stop: asyncio.Event | None = None,
    clock: Clock | None = None,
) -> int:
    if settings.environment == DEMO:
        raise ConfigError("the demo environment runs synthetic data: use --demo")
    validate_roles(settings)  # a misconfigured enabled LLM role fails startup (exit 2)
    mode = settings.execution_mode
    if mode not in PAPER_MODES:
        logger.warning("EXECUTION_MODE=%s ignored: the v2 engine trades on the simulated "
                       "broker only (paper)", mode)  # fmt: skip
    clock = clock or WallClock()
    calendar = get_calendar()
    limits = load_risk_limits()
    experiment = load_experiment(settings.experiment_file or DEFAULT_EXPERIMENT_PATH)
    config = engine_config(experiment, environment=settings.environment, limits=limits,
                           halt_file=settings.halt_file)  # fmt: skip
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with EventStore(settings.db_path) as store:
        sink = StoreSink(store, clock, "engine")
        reference = await refresh_reference(settings.reference_dir, now_ist(clock).date())
        alert_reference(reference, sink)
        universe = list(reference.instruments.by_symbol.values())
        priced = {i.key: i for i in universe}
        for book_id in config.books:
            priced |= held_instruments(store, book_id)
        # One Laya for the classifier and Book B; every answer is cached for replays (M8.5).
        cascade = build_cascade(settings, sink=sink, cache=StoreResponseCache(store, DM_CACHE))
        router = build_router(settings, clock=clock, sink=sink, store=store)
        tape = TapeWriter(settings.tape_dir)
        quotes = YFinanceQuoteSource(
            list(priced.values()), clock=clock, sink=sink, tape=tape,
            validator=QuoteValidator(band_lookup(priced.values())),
            market_open=calendar.is_market_open,
        )  # fmt: skip
        engine = build_engine(
            config=config, clock=clock, calendar=calendar, store=store, quotes=quotes,
            history=YFinanceHistorySource(tape=tape), universe=universe, limits=limits,
            announcements=_announcements(settings, store, clock, sink, priced, cascade),
        )  # fmt: skip
        advisors = build_advisors(experiment, sink=sink, clock=clock, calendar=calendar,
                                  cascade=cascade, router=router, events=engine.events_for,
                                  regime=lambda: engine.regime)  # fmt: skip
        for book_id, advisor in advisors.items():
            engine.set_advisor(book_id, advisor)
        engine.reporter = make_reporter(engine, experiment, settings, settings.reports_dir,
                                    notify=True, router=router)  # fmt: skip
        return await _drive(engine, view, stop)


def _announcements(
    settings: Settings,
    store: EventStore,
    clock: Clock,
    sink: StoreSink,
    instruments: dict[str, Instrument],
    cascade: Cascade | None,
) -> AnnouncementPipeline | None:
    if not settings.announcements_enabled:
        return None
    stored = [p for e in store.read(types=["AnnouncementReceived"])
              if isinstance(p := e.payload, AnnouncementReceived)]  # fmt: skip
    seen, newest = watermark(stored)
    equities = [i for i in instruments.values() if i.series == "EQ"]
    ingestor = AnnouncementIngestor(instruments=equities, clock=clock, sink=sink,
                                    url=settings.announcements_url, seen=seen,
                                    last_newest=newest)  # fmt: skip
    if cascade is None:
        logger.warning("no decision model (laya not installed, no TYPESAFE_API_KEY): "
                       "announcements are stored but not classified")  # fmt: skip
        sink.emit(Alert(level="WARNING", key="decision_models_unavailable",
                        message="announcements stored, not classified"), source="engine")  # fmt: skip
        return AnnouncementPipeline(ingestor=ingestor, classifier=None)
    classified = [r["event_id"] for r in store.query("SELECT event_id FROM typed_events")]
    classifier = EventClassifier(cascade=cascade, sink=sink, clock=clock)
    return AnnouncementPipeline(ingestor=ingestor, classifier=classifier,
                                backlog=unclassified(stored, classified))  # fmt: skip


async def run_demo(
    settings: Settings,
    view: SessionView,
    *,
    stop: asyncio.Event | None = None,
    step_s: float = 30.0,
    wall_s: float = 0.1,
    today: date | None = None,
) -> int:
    if settings.environment != DEMO:
        raise ConfigError("the demo runs only in ENVIRONMENT=demo (its own state directory)")
    validate_roles(settings)
    calendar = get_calendar()
    today = today or datetime.now(IST).date()
    days = calendar.trading_days(date(today.year, 1, 1), date(today.year, 12, 31))
    day, previous = demo_day(today, days)
    bars, quotes = synthetic_day(day, previous)
    clock = ReplayClock(datetime.combine(day, time(9, 0), IST))
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    path = settings.state_dir / "demo.db"
    for stale in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        stale.unlink(missing_ok=True)  # every demo starts from a clean book
    limits = load_risk_limits()
    experiment = load_experiment(settings.experiment_file or DEFAULT_EXPERIMENT_PATH)
    config = engine_config(experiment, environment=settings.environment, limits=limits,
                           halt_file=settings.halt_file)  # fmt: skip
    with EventStore(path) as store:
        engine = build_engine(
            config=config, clock=clock, calendar=calendar, store=store,
            quotes=TapeQuoteSource(quotes, clock=clock), history=TapeHistorySource(bars),
            universe=demo_instruments(), limits=limits,
        )  # fmt: skip
        sink = StoreSink(store, clock, "engine")
        advisors = build_advisors(experiment, sink=sink, clock=clock, calendar=calendar,
                                  cascade=None, router=None, events=engine.events_for,
                                  regime=lambda: engine.regime)  # fmt: skip
        for book_id, advisor in advisors.items():
            engine.set_advisor(book_id, advisor)
        engine.reporter = make_reporter(engine, experiment, settings,
                                    settings.state_dir / "reports", notify=False)  # fmt: skip
        done = asyncio.Event()
        exit_at = datetime.combine(day, time(16, 0), IST)
        pacer = asyncio.create_task(pace(clock, exit_at, step_s=step_s, wall_s=wall_s, done=done))
        try:
            return await _drive(engine, view, stop)
        finally:
            done.set()
            await asyncio.gather(pacer, return_exceptions=True)


_PENDING: set[asyncio.Task[bool]] = set()  # fire-and-forget summaries (kept from GC)


def make_reporter(
    engine: Engine,
    experiment: ExperimentConfig,
    settings: Settings,
    reports_dir: Path,
    *,
    notify: bool,
    router: LLMRouter | None = None,
) -> Callable[[date], Awaitable[None]]:
    """The REPORT step: the daily report to ``reports_dir``, a Telegram summary (5 s timeout,
    fire-and-forget: the report is on disk whether or not the message goes out), then the nightly
    review when a ``router`` is given (role ``review``; a no-op when the role is not configured)."""

    async def report(day: date) -> None:
        lows = {b: float(book.tracker.state.low_equity) for b, book in engine.books.items()
                if book.tracker.state is not None}  # fmt: skip
        inputs = ReportInputs(
            day=day, capital=experiment.capital_inr, books=engine.config.books,
            advisors={b: spec.advisor for b, spec in experiment.books.items()},
            experiment=experiment.experiment, nifty_closes=engine.index_closes(),
            equal_weight_day_pct=engine.equal_weight_day_pct(),
            infra_cost_inr_per_day=settings.infra_cost_inr_per_day, intraday_low_equity=lows,
        )  # fmt: skip
        result = build_report(engine.store, inputs, engine.calendar)
        _, md_path = write_report(result, reports_dir)
        engine.sink.emit(Alert(level="INFO", key="daily_report", message=f"written {md_path.name}"),
                         source="engine")  # fmt: skip
        if notify:
            notifier = TelegramNotifier()
            if notifier.enabled:
                task = asyncio.create_task(send_summary(telegram_summary(result),
                                                        notifier.send_message))  # fmt: skip
                _PENDING.add(task)
                task.add_done_callback(_PENDING.discard)
        if router is not None:
            try:
                await review_day(engine.store, router, day, clock=engine.clock, sink=engine.sink)
            except Exception as exc:  # the review is advisory: never fails the report
                logger.exception("nightly review failed")
                engine.sink.emit(Alert(level="WARNING", key="nightly_review_failed",
                                       message=f"{type(exc).__name__}: {exc}"),
                                 source="engine")  # fmt: skip

    return report


async def _drive(engine: Engine, view: SessionView, stop: asyncio.Event | None) -> int:
    """Run the engine; refresh the view every second; stop early when ``stop`` is set."""
    stats = getattr(view, "stats", None)
    projector = StatsProjector(stats, engine) if stats is not None else None
    view.set_effective_mode("local_paper")
    run = asyncio.create_task(engine.run(), name="engine")

    async def refresh() -> None:
        while not run.done():
            if projector is not None:
                try:
                    projector.refresh()
                except Exception:  # the view never stops trading
                    logger.exception("view refresh failed")
            await view.render()
            await asyncio.sleep(1.0)

    painter = asyncio.create_task(refresh(), name="view")
    stopper = asyncio.create_task(stop.wait() if stop else asyncio.Event().wait())
    async with view:
        await asyncio.wait({run, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if not run.done():  # asked to stop
            run.cancel()
        stopper.cancel()
        results = await asyncio.gather(run, stopper, return_exceptions=True)
        painter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await painter
        if projector is not None:
            projector.refresh()
        await view.render()
    outcome = results[0]
    if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
        raise outcome
    return outcome if isinstance(outcome, int) else 0
