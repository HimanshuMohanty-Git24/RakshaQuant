"""Plan M5.6 (part 3), M12.2: the front ends drive the v2 engine; a view only paints it."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest

import src.engine.live as live
from src.config.errors import ConfigError
from src.domain.clock import ReplayClock
from src.domain.types import MarketDataSource
from src.engine.demo import DEMO_SYMBOLS, demo_instruments, pace, synthetic_day
from src.engine.runner import Engine
from src.marketdata.announcements import PollStats
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.reference.instruments import InstrumentSet
from src.reference.refresh import ReferenceData
from src.store.event_store import EventStore
from src.utils.market_time import IST


class RecordingView:
    """An :class:`~src.engine.live.EngineView` that counts its paints."""

    def __init__(self) -> None:
        self.paints = 0
        self.opened = self.closed = False
        self.engine: Engine | None = None

    async def __aenter__(self) -> RecordingView:
        self.opened = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def paint(self, engine: Engine) -> None:
        assert self.opened and not self.closed  # painted only while entered
        self.paints += 1
        self.engine = engine


APPROVED_ENTRIES = ("SELECT COUNT(*) AS n FROM decisions WHERE kind IN ('open', 'increase') "
                    "AND outcome IN ('APPROVED', 'RESIZED')")  # fmt: skip


def approved_entries(path: Any) -> int:
    with EventStore(path) as store:
        return int(store.query(APPROVED_ENTRIES)[0]["n"])


def env(settings: Any, environment: str, tmp_path: Any) -> Any:
    return settings.model_copy(
        update={"environment": environment, "state_dir": tmp_path / "var" / environment}
    )


async def test_the_demo_trades_a_synthetic_day_and_the_view_is_painted(settings, tmp_path):
    view = RecordingView()
    demo = env(settings, "demo", tmp_path)
    code = await live.run_demo(demo, view, step_s=60.0, wall_s=0.0)
    assert code == 0 and view.opened and view.closed and view.paints >= 2
    assert view.engine is not None
    assert {q.source for q in view.engine.market.quotes().values()} == {MarketDataSource.SIMULATED}
    assert approved_entries(live.demo_store_path(demo)) >= 1
    with EventStore(live.demo_store_path(demo)) as store:
        assert store.read(types=["SignalGenerated"], limit=1)
        traded = store.query("SELECT COUNT(*) AS n FROM fills")[0]["n"]
        assert traded >= 1


async def test_a_run_needs_no_view(settings, tmp_path):
    demo = env(settings, "demo", tmp_path)
    assert await live.run_demo(demo, step_s=120.0, wall_s=0.0) == 0  # the web console's way
    assert approved_entries(live.demo_store_path(demo)) >= 1


async def test_each_demo_starts_from_a_clean_book(settings, tmp_path):
    demo = env(settings, "demo", tmp_path)
    await live.run_demo(demo, RecordingView(), step_s=120.0, wall_s=0.0)
    await live.run_demo(demo, RecordingView(), step_s=120.0, wall_s=0.0)
    assert approved_entries(live.demo_store_path(demo)) >= 1  # not duplicates of the first run


async def test_environments_are_never_mixed(settings, tmp_path):
    with pytest.raises(ConfigError, match="demo"):
        await live.run_demo(env(settings, "paper", tmp_path), RecordingView())
    with pytest.raises(ConfigError, match="--demo"):
        await live.run_paper(env(settings, "demo", tmp_path), RecordingView())


async def test_a_stop_request_ends_the_run_cleanly(settings, tmp_path):
    stop = asyncio.Event()
    view = RecordingView()
    task = asyncio.create_task(
        live.run_demo(env(settings, "demo", tmp_path), view, stop=stop, step_s=30.0, wall_s=0.05)
    )
    await asyncio.sleep(0.3)
    stop.set()
    assert await asyncio.wait_for(task, 30) == 0
    assert view.closed


async def test_the_paper_run_wires_reference_data_yfinance_and_the_tape(
    settings, tmp_path, monkeypatch
):
    """run_paper with the network replaced: reference → universe, YFinance → a replayed day."""
    day, previous = date(2026, 10, 5), date(2026, 10, 1)
    bars, quotes = synthetic_day(day, previous)
    quotes = [q.model_copy(update={"source": MarketDataSource.YFINANCE}) for q in quotes]
    bars = [b.model_copy(update={"source": MarketDataSource.YFINANCE}) for b in bars]
    clock = ReplayClock(datetime.combine(day, time(9, 0), IST))
    seen: dict[str, Any] = {}

    async def reference(directory: Any, today: date, **kw: Any) -> ReferenceData:
        seen["reference_day"] = today
        instruments = {i.symbol: i for i in demo_instruments()}
        return ReferenceData([], today, InstrumentSet(instruments, ()))

    def quote_source(instruments: list[Any], **kw: Any) -> TapeQuoteSource:
        seen["priced"] = sorted(i.key for i in instruments)
        seen["tape"] = kw["tape"]
        return TapeQuoteSource(quotes, clock=clock)

    monkeypatch.setattr(live, "refresh_reference", reference)
    monkeypatch.setattr(live, "YFinanceQuoteSource", quote_source)
    monkeypatch.setattr(live, "YFinanceHistorySource", lambda **kw: TapeHistorySource(bars))
    polls: list[tuple[datetime, bool]] = []

    class FakeAnnouncements:
        interval_s = 300.0

        def __init__(self, *, instruments: Any, url: str, **kw: Any) -> None:
            seen["announced"] = sorted(i.key for i in instruments)
            seen["url"] = url

        async def poll(self, *, force: bool = False) -> PollStats:
            polls.append((clock.now(), force))
            return PollStats(ok=True)

    monkeypatch.setattr(live, "AnnouncementIngestor", FakeAnnouncements)
    paper = env(settings, "paper", tmp_path).model_copy(update={"announcements_enabled": True})
    view = RecordingView()
    task = asyncio.create_task(live.run_paper(paper, view, clock=clock))
    await pace(clock, datetime.combine(day, time(16, 0), IST), step_s=60.0, wall_s=0.0)
    assert await asyncio.wait_for(task, 60) == 0
    assert seen["reference_day"] == day
    assert seen["priced"] == sorted(f"NSE:EQ:{s}" for s, *_ in DEMO_SYMBOLS)
    assert seen["announced"] == seen["priced"] and seen["url"].endswith("Online_announcements.xml")
    assert paper.db_path.exists() and approved_entries(paper.db_path) >= 1 and view.paints >= 1
    assert polls[0][1] is True and polls[0][0] < datetime.combine(day, time(9, 15), IST)  # backfill
    in_session = [t for t, force in polls if not force]
    assert len(in_session) >= 70  # every 5 minutes from the open to the close
    assert all(b - a >= timedelta(minutes=5) for a, b in zip(in_session, in_session[1:]))
