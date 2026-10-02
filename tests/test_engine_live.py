"""Plan M5.6 (part 3): the front ends drive the v2 engine; views are fed from projections."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest

import src.engine.live as live
from src.config.errors import ConfigError
from src.dashboard.cli import TradingStats
from src.domain.clock import ReplayClock
from src.domain.types import MarketDataSource
from src.engine.demo import DEMO_SYMBOLS, demo_day, demo_instruments, pace, synthetic_day
from src.live.views import SessionView
from src.marketdata.announcements import PollStats
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.reference.instruments import InstrumentSet
from src.reference.refresh import ReferenceData
from src.store.event_store import EventStore
from src.utils.market_time import IST

TODAY = date(2026, 10, 2)  # a holiday: the demo replays the next session, Mon 5 Oct


class RecordingView(SessionView):
    def __init__(self) -> None:
        self.stats = TradingStats()
        self.renders = 0
        self.mode = ""
        self.opened = self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def render(self) -> None:
        self.renders += 1

    async def wait(self, seconds: int) -> None:
        await asyncio.sleep(0)

    def note(self, message: str) -> None:
        pass

    def set_effective_mode(self, mode: str) -> None:
        self.mode = mode

    async def emit_cycle(self, trace: Any) -> None:
        pass


def env(settings: Any, environment: str, tmp_path: Any) -> Any:
    return settings.model_copy(
        update={"environment": environment, "state_dir": tmp_path / "var" / environment}
    )


async def test_the_demo_trades_a_synthetic_day_and_the_view_shows_it(settings, tmp_path):
    view = RecordingView()
    demo = env(settings, "demo", tmp_path)
    code = await live.run_demo(demo, view, step_s=60.0, wall_s=0.0, today=TODAY)
    assert code == 0 and view.opened and view.closed and view.renders >= 2
    assert view.mode == "local_paper"
    stats = view.stats
    assert stats.signals_generated > 0 and stats.trades_approved >= 1
    assert stats.total_trades >= 1 or stats.open_positions  # it traded
    assert stats.data_source == MarketDataSource.SIMULATED.value
    assert any(e["message"].startswith("Session") for e in stats.activity_log)
    assert (demo.state_dir / "demo.db").exists()
    with EventStore(demo.state_dir / "demo.db") as store:
        assert store.query("SELECT COUNT(*) AS n FROM decisions")[0]["n"] >= 1


async def test_each_demo_starts_from_a_clean_book(settings, tmp_path):
    demo = env(settings, "demo", tmp_path)
    await live.run_demo(demo, RecordingView(), step_s=120.0, wall_s=0.0, today=TODAY)
    view = RecordingView()
    await live.run_demo(demo, view, step_s=120.0, wall_s=0.0, today=TODAY)
    assert view.stats.trades_approved >= 1  # not blocked as duplicates of the first run


async def test_environments_are_never_mixed(settings, tmp_path):
    with pytest.raises(ConfigError, match="demo"):
        await live.run_demo(env(settings, "paper", tmp_path), RecordingView())
    with pytest.raises(ConfigError, match="--demo"):
        await live.run_paper(env(settings, "demo", tmp_path), RecordingView())


async def test_a_stop_request_ends_the_run_cleanly(settings, tmp_path):
    stop = asyncio.Event()
    view = RecordingView()
    task = asyncio.create_task(
        live.run_demo(
            env(settings, "demo", tmp_path), view, stop=stop, step_s=30.0, wall_s=0.05, today=TODAY
        )
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
    assert paper.db_path.exists() and view.stats.trades_approved >= 1
    assert polls[0][1] is True and polls[0][0] < datetime.combine(day, time(9, 15), IST)  # backfill
    in_session = [t for t, force in polls if not force]
    assert len(in_session) >= 70  # every 5 minutes from the open to the close
    assert all(b - a >= timedelta(minutes=5) for a, b in zip(in_session, in_session[1:]))


def test_demo_day_picks_the_next_session():
    days = [date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 6)]
    assert demo_day(date(2026, 10, 3), days) == (date(2026, 10, 5), date(2026, 10, 1))
    assert demo_day(date(2026, 10, 6), days) == (date(2026, 10, 6), date(2026, 10, 5))
    assert demo_day(date(2026, 12, 31), days) == (date(2026, 10, 6), date(2026, 10, 5))
