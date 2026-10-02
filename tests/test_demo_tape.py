"""Plan M9.6: the demo replays the bundled fixture tape through the real engine - identically."""

from __future__ import annotations

from src.engine import live
from src.engine.demo import DEMO_DAY, DEMO_PREVIOUS, DEMO_TAPE, synthetic_day, write_demo_tape
from src.engine.replay import canonical_events
from src.store.event_store import EventStore
from src.store.tape import read_bars, read_quotes
from src.utils.market_time import IST
from src.web.run_manager import RunControlError, RunManager
from tests.test_engine_live import RecordingView, env

SRC = DEMO_TAPE.parents[1]


def test_the_bundled_tape_is_what_the_generator_makes(tmp_path):
    bars, quotes = synthetic_day(DEMO_DAY, DEMO_PREVIOUS)
    assert read_quotes(DEMO_TAPE, DEMO_DAY) == quotes  # else: scripts/build_demo_tape.py
    assert read_bars(DEMO_TAPE, DEMO_DAY) == bars
    written = write_demo_tape(tmp_path)
    assert sorted(p.name for p in written) == ["bars.parquet", "quotes.parquet"]
    assert read_quotes(tmp_path, DEMO_DAY) == quotes


async def test_every_demo_run_replays_the_same_day(settings, tmp_path):
    runs = []
    for name in ("one", "two"):
        demo = env(settings, "demo", tmp_path / name)
        assert await live.run_demo(demo, RecordingView(), step_s=120.0, wall_s=0.0) == 0
        with EventStore(live.demo_store_path(demo)) as store:
            runs.append(canonical_events(store))
    assert runs[0] == runs[1] and len(runs[0]) > 100
    assert '"type": "FillReceived"' in "\n".join(runs[0])  # it trades
    assert f'"session_date": "{DEMO_DAY}"' in "\n".join(runs[0])


async def test_demo_and_paper_runs_never_cross_environments(settings, tmp_path):
    paper = RunManager(env(settings, "paper", tmp_path))
    try:
        await paper.start(demo=True)
        raise AssertionError("a demo started outside the demo environment")
    except RunControlError as exc:
        assert "--demo" in str(exc)
    demo = RunManager(env(settings, "demo", tmp_path))
    try:
        await demo.start(demo=False)
        raise AssertionError("a paper run started in the demo environment")
    except RunControlError as exc:
        assert "demo run" in str(exc)


def test_nothing_fabricates_a_session_for_the_ui():
    """The old order-free UI generator (random prices, invented trades) is gone for good."""
    text = (SRC / "web" / "run_manager.py").read_text(encoding="utf-8")
    for marker in ("_demo_loop", "random.", "_demo_cycle", "[SHADOW]"):
        assert marker not in text


async def test_the_demo_runs_while_the_entry_point_holds_the_process_store(settings, tmp_path):
    """Regression (found by the M9.6 smoke run): the entry point keeps the environment's
    db_path open for process events; the demo must not try to delete that file."""
    demo = env(settings, "demo", tmp_path)
    with EventStore(demo.db_path):  # what run_entry_point(record_events=True) holds
        assert await live.run_demo(demo, RecordingView(), step_s=300.0, wall_s=0.0) == 0
        assert await live.run_demo(demo, RecordingView(), step_s=300.0, wall_s=0.0) == 0
    assert live.demo_store_path(demo) != demo.db_path


async def test_after_a_demo_the_api_keeps_the_demo_s_clock(settings, tmp_path):
    demo = env(settings, "demo", tmp_path)
    assert await live.run_demo(demo, RecordingView(), step_s=300.0, wall_s=0.0) == 0
    manager = RunManager(demo)
    try:
        summary = manager.queries().summary(None, running=False)
        assert summary.now.astimezone(IST).date() == DEMO_DAY  # not the wall clock's today
        assert summary.session is not None and summary.session.state == "EXIT"
        assert all(b.trades_today >= 1 and b.valuation == "last_mark" for b in summary.books)
    finally:
        manager.close_reader()


async def test_the_demo_shows_a_veto_that_avoided_a_loss(settings, tmp_path):
    """No model runs in the demo: the typed-veto book (B) vetoes TCS by script, and says so."""
    demo = env(settings, "demo", tmp_path)
    assert await live.run_demo(demo, RecordingView(), step_s=300.0, wall_s=0.0) == 0
    with EventStore(live.demo_store_path(demo)) as store:
        trades = {(r["book_id"], r["instrument_key"]): r["net_pnl"]
                  for r in store.query("SELECT book_id, instrument_key, net_pnl FROM trades")}  # fmt: skip
        verdicts = [e.payload for e in store.read(types=["AdvisorVerdict"], book_id="B")]
    assert ("A", "NSE:EQ:TCS") in trades and float(trades[("A", "NSE:EQ:TCS")]) < 0  # a loser
    assert ("B", "NSE:EQ:TCS") not in trades and ("B", "NSE:EQ:INFY") in trades
    veto = next(v for v in verdicts if v.verdict.value == "VETO")
    assert veto.model == "demo-script" and "scripted demo veto" in veto.reasons[0].claim
    manager = RunManager(demo)  # after the run: the API reads the tape the demo replayed
    try:
        watch = {w.symbol: w for w in manager.queries().watchlist(None)}
        assert len(watch) == 8 and watch["TCS"].ltp is not None
        assert "momentum BUY" in watch["TCS"].signals_today
    finally:
        manager.close_reader()
