"""
Replaying a recorded day (plan M8.5; audit §G.4): the same engine, books, advisors and report, on
a :class:`ReplayClock`, from what the system recorded - no network.

* Market data: the day's tape (``<var>/tape/<date>``: quotes received and the daily history).
* Universe: the reference snapshots cached for that day (sectors, ticks, bands); failing that,
  the instruments on the tape.
* Announcements: those the live run stored, released when they were received.
* AI: cached responses only - the LLM router's reply cache and the decision-model answer cache
  of the source store; a miss is a provider error, so the advisor abstains rather than giving a
  new, different answer.
* Output: a fresh event store (``replay.db``) and the day's report in ``out_dir``.

:func:`canonical_events` renders a store's events for golden comparisons: generated ids become
``ID1, ID2, ...`` in order of appearance, and wall-clock measurements (heartbeats, latencies)
are left out.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from src.config.limits import load_risk_limits
from src.config.settings import Settings
from src.decision_models.adapters import CONTEXT_TOKENS
from src.decision_models.cache import CachedDecisionModel, ReplayStub
from src.decision_models.cascade import Cascade, CascadeConfig
from src.decision_models.tasks.announcements import AnnouncementPipeline, EventClassifier
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.events import AnnouncementReceived, SessionStateChanged
from src.domain.sink import EventSink
from src.domain.types import Instrument, SessionState
from src.engine.demo import DEMO_TAPE, ScriptedVeto, demo_instruments
from src.engine.live import DEMO, DM_CACHE, make_reporter
from src.engine.runner import Engine, build_engine
from src.evaluation.books import build_advisors, engine_config
from src.evaluation.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment
from src.llm.pricing import PricingTable
from src.llm.registry import role_configs
from src.llm.router import BudgetLimits, LLMRouter, StoreResponseCache
from src.llm.types import LLMServerError
from src.marketdata.announcements import PollStats
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.reference.refresh import load_reference_offline
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.store.tape import read_bars, read_quotes
from src.utils.market_time import IST

START, END = time(8, 50), time(16, 0)
_GENERATED_ID = re.compile(r"\b[0-9a-f]{24}\b")  # src.domain.ids.new_id
_WALL_CLOCK = {"latency_ms", "uptime_s", "loop_lag_ms", "pid"}


class _ReadOnlyCache:
    def __init__(self, store: EventStore | None, namespace: str) -> None:
        self._inner = StoreResponseCache(store, namespace) if store is not None else None

    def get(self, key: str) -> str | None:
        return self._inner.get(key) if self._inner is not None else None

    def put(self, key: str, value: str, ts: datetime) -> None:
        """Replays never write to the source store."""


class _NoNetwork:
    def get(self, provider: str) -> Any:
        return self

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise LLMServerError("replay: no network (cache miss)")


@dataclass
class _RecordedAnnouncements:
    """Releases the live run's announcements as the replay clock passes their receipt time."""

    announcements: Sequence[AnnouncementReceived]
    clock: ReplayClock
    sink: EventSink
    interval_s: float = 300.0
    _released: int = 0

    async def poll(self, *, force: bool = False) -> PollStats:
        now = self.clock.now()
        ordered = sorted(self.announcements, key=lambda a: a.received_at)
        new = []
        while self._released < len(ordered) and ordered[self._released].received_at <= now:
            item = ordered[self._released]
            self.sink.emit(item, source="announcements")
            new.append(item)
            self._released += 1
        return PollStats(ok=True, fetched=len(new), new=new)


async def replay_day(
    settings: Settings,
    day: date,
    *,
    out_dir: Path,
    source_db: Path | None = None,
    tape_dir: Path | None = None,
    universe: Sequence[Instrument] | None = None,
    step_s: float = 30.0,
) -> Path:
    """Replay ``day``; returns the replay's event store path. In the ``demo`` environment a
    recording of the demo is replayed as the demo ran it: its bundled tape, its instruments and
    book B's scripted veto (no model runs in the demo)."""
    demo = settings.environment == DEMO
    tape_root = tape_dir or (DEMO_TAPE if demo else settings.tape_dir)
    quotes, bars = read_quotes(tape_root, day), read_bars(tape_root, day)
    if not quotes:
        raise FileNotFoundError(f"no quotes on the tape for {day} under {tape_root}")
    instruments = list(universe or (demo_instruments() if demo else
                                    _universe(settings, day, quotes)))  # fmt: skip
    experiment = load_experiment(settings.experiment_file or DEFAULT_EXPERIMENT_PATH)
    limits = load_risk_limits()
    config = engine_config(experiment, environment=settings.environment, limits=limits)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "replay.db"
    for stale in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        stale.unlink(missing_ok=True)
    source = EventStore(source_db) if source_db is not None and source_db.exists() else None
    try:
        recorded = [] if source is None else [
            p for e in source.read(types=["AnnouncementReceived"])
            if isinstance(p := e.payload, AnnouncementReceived)
            and p.received_at.astimezone(IST).date() == day
        ]  # fmt: skip
        clock = ReplayClock(_started_at(source, day) or datetime.combine(day, START, IST))
        with EventStore(db) as store:
            sink = StoreSink(store, clock, "engine")
            cascade = _replay_cascade(settings, sink, source)
            pipeline = AnnouncementPipeline(
                ingestor=_RecordedAnnouncements(recorded, clock, sink),
                classifier=EventClassifier(cascade=cascade, sink=sink, clock=clock),
            )  # fmt: skip
            engine = build_engine(
                config=config, clock=clock, calendar=get_calendar(), store=store,
                quotes=TapeQuoteSource(quotes, clock=clock), history=TapeHistorySource(bars),
                universe=instruments, limits=limits, announcements=pipeline,
            )  # fmt: skip
            router = LLMRouter(roles=role_configs(settings), clients=_NoNetwork(),
                               pricing=PricingTable.from_yaml(), sink=sink, clock=clock,
                               usd_inr=settings.usd_inr, timeout_s=settings.llm_timeout_s,
                               budgets=BudgetLimits(), cache=_ReadOnlyCache(source, "llm_cache"))  # fmt: skip
            advisors = build_advisors(
                experiment, sink=sink, clock=clock, calendar=engine.calendar, cascade=cascade,
                router=router, events=engine.events_for, regime=lambda: engine.regime,
            )  # fmt: skip
            if demo:
                for book_id, spec in experiment.books.items():
                    if spec.advisor == "typed_veto":
                        advisors[book_id] = ScriptedVeto(book_id=book_id, sink=sink)
            for book_id, advisor in advisors.items():
                engine.set_advisor(book_id, advisor)
            engine.reporter = make_reporter(engine, experiment, settings, out_dir, notify=False)
            await _run(engine, clock, datetime.combine(day, END, IST), step_s)
    finally:
        if source is not None:
            source.close()
    return db


def _started_at(source: EventStore | None, day: date) -> datetime | None:
    """When the recorded session began (its ``PRE_OPEN``), so the replay keeps its timeline."""
    if source is None:
        return None
    for event in source.read(types=[SessionStateChanged.event_type], ist_date=day):
        p = event.payload
        if isinstance(p, SessionStateChanged) and p.current is SessionState.PRE_OPEN:
            return event.ts_utc
    return None


def _replay_cascade(settings: Settings, sink: EventSink, source: EventStore | None) -> Cascade:
    cache = _ReadOnlyCache(source, DM_CACHE)
    checkpoint = settings.decision_laya_checkpoint
    laya = CachedDecisionModel(ReplayStub("laya", checkpoint, CONTEXT_TOKENS[checkpoint]), cache,
                               replay_only=True)  # fmt: skip
    jev = CachedDecisionModel(ReplayStub("jev", settings.typesafe_model, 64_000), cache,
                              replay_only=True)  # fmt: skip
    return Cascade(laya=laya, jev=jev, sink=sink, config=CascadeConfig(
        escalate_band=(settings.decision_escalate_low, settings.decision_escalate_high),
        shadow_pct=settings.decision_shadow_pct))  # fmt: skip


def _universe(settings: Settings, day: date, quotes: Iterable[Any]) -> list[Instrument]:
    reference = load_reference_offline(settings.reference_dir, day)
    if reference is not None:
        return list(reference.by_symbol.values())
    keys = sorted({q.instrument_key for q in quotes})
    return [
        Instrument.nse_equity(key.split(":", 2)[2]) for key in keys if key.startswith("NSE:EQ:")
    ]


async def _run(engine: Engine, clock: ReplayClock, until: datetime, step_s: float) -> None:
    task = asyncio.create_task(engine.run())
    while not task.done() and clock.now() < until:
        await clock.advance(step_s)
    if not task.done():
        await clock.advance(step_s)
    await task


def canonical_events(
    store: EventStore, *, skip: Iterable[str] = ("Heartbeat", "LoopLag")
) -> list[str]:
    """The store's events as canonical JSON lines (stable across runs of the same input)."""
    skipped = set(skip)
    ids: dict[str, str] = {}

    def rename(match: re.Match[str]) -> str:
        return ids.setdefault(match.group(0), f"ID{len(ids) + 1}")

    lines = []
    for event in store.read():
        if event.type in skipped:
            continue
        record = event.to_dict()
        record.pop("seq", None)  # the line order is the order
        payload = record.get("payload")
        if isinstance(payload, dict):
            for name in _WALL_CLOCK & payload.keys():
                payload[name] = None
        text = json.dumps(record, sort_keys=True, default=str)
        lines.append(_GENERATED_ID.sub(rename, text))
    return lines
