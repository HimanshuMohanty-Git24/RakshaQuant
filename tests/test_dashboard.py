"""Plan M12.2: the CLI dashboard renders the projections - books, risk, positions, decisions."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from rich.console import Console

import src.engine.live as live
from src.dashboard.cli import Frame, TerminalView, read_frame, render
from src.store.event_store import EventStore
from src.web.models import (
    AlertRow,
    BookRisk,
    BookSummary,
    DecisionRow,
    KillSwitchRow,
    PositionRow,
    RiskView,
    ScheduleStep,
    SessionInfo,
    Summary,
    Utilisation,
)
from src.web.queries import build_queries, live_view
from tests.test_books import run as run_day
from tests.test_books import three_books

NOW = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # 10:30 IST


def text_of(frame: Frame) -> str:
    console = Console(file=io.StringIO(), width=220, record=True)
    console.print(render(frame))
    return console.export_text()


def book(book_id: str, advisor: str, switch: str = "ARMED") -> BookSummary:
    return BookSummary(
        book_id=book_id, advisor=advisor, equity=Decimal("1003825.41"), cash=Decimal("900000"),
        unrealized_pnl=Decimal("-1200.5"), day_pnl=Decimal("3825.41"), valued_at=NOW,
        valuation="live", realized_pnl_today=Decimal("5025.91"), open_positions=1,
        open_orders=1, trades_today=2, kill_switch=switch, day_return_pct=0.38,
    )  # fmt: skip


def risk_of(book_id: str, *, tripped: bool = False, used: str = "0.25") -> BookRisk:
    switches = [KillSwitchRow(scope="GLOBAL", name="global", state="HALT_NEW", reason="operator",
                              actor="web", since=NOW)] if tripped else []  # fmt: skip
    return BookRisk(
        book_id=book_id, kill_switches=switches, daily_state=None,
        rejections_today={"MAX_POSITIONS": 3} if tripped else {}, equity=None, valuation="live",
        utilisation=[
            Utilisation(key="GROSS", label="gross", used=Decimal(used) * 100, limit=Decimal(100),
                        unit="ratio", fraction=float(used)),
            Utilisation(key="POS", label="positions", used=Decimal(1), limit=Decimal(10),
                        unit="count", fraction=0.1),
        ], sectors=[], event_blocks=[],
    )  # fmt: skip


def a_frame() -> Frame:
    summary = Summary(
        environment="paper", demo=False, running=True, experiment="month1-2026-10",
        session=SessionInfo(date=date(2026, 10, 5), state="MONITOR"), market_open=True, now=NOW,
        last_seq=1234, books=[book("A", "none"), book("B", "typed_veto", switch="HALT_NEW")],
        schedule=[ScheduleStep(state="CLOSE", at=datetime(2026, 10, 5, 10, 0, tzinfo=UTC))],
    )  # fmt: skip
    risk = RiskView(limits_hash="abc", limits={},
                    books=[risk_of("A"), risk_of("B", tripped=True, used="1.0")])  # fmt: skip
    position = PositionRow(
        book_id="A", instrument_key="NSE:EQ:INFY", symbol="INFY", product="CNC", quantity=60,
        avg_price=Decimal("1500.10"), realized_pnl=Decimal(0), mark=Decimal("1480.09"),
        unrealized_pnl=Decimal("-1200.5"), updated_ts=NOW, strategy="momentum",
        entry_decision_id="d1", stop_price=Decimal("1440"), target_price=Decimal("1590"),
        entered_on=date(2026, 10, 1), held_sessions=2,
    )  # fmt: skip
    decision = DecisionRow(seq=9, ts=NOW, decision_id="d2", signal_id="s2", book_id="B",
                           instrument_key="NSE:EQ:TCS", symbol="TCS", strategy="mean_reversion",
                           disposition="vetoed", detail="typed_veto 0.71 >= 0.6",
                           client_order_id=None)  # fmt: skip
    alert = AlertRow(seq=7, ts=NOW, level="WARNING", key="stale", message="quotes are 95 s old",
                     book_id=None)  # fmt: skip
    return Frame(summary=summary, risk=risk, positions=[position], decisions=[decision],
                 alerts=[alert], quote_age_s={"yfinance": 2.4}, tasks=("market", "monitor"),
                 halt_file="var/paper/HALT")  # fmt: skip


def test_a_frame_shows_the_books_risk_positions_decisions_and_warnings():
    text = text_of(a_frame())
    assert "PAPER" in text and "month1-2026-10" in text and "MONITOR" in text
    assert "10:30:00 IST" in text and "next CLOSE 15:30:00" in text and "running" in text
    assert "1,003,825.41" in text and "+3,825.41" in text and "+0.38%" in text
    assert "typed_veto" in text and "HALT_NEW" in text
    assert "global:global HALT_NEW" in text and "MAX_POSITIONS x3" in text
    assert "gross 25%" in text and "gross 100%" in text and "positions 1/10" in text
    assert "INFY" in text and "1,440.00" in text and "1,590.00" in text and "-1,200.50" in text
    assert "TCS" in text and "vetoed" in text and "typed_veto 0.71 >= 0.6" in text
    assert "quotes are 95 s old" in text and "yfinance 2s old" in text
    assert "var/paper/HALT" in text


async def test_a_recorded_session_reads_and_renders_from_the_projections(settings, tmp_path):
    with three_books(tmp_path) as (engine, clock):
        assert await run_day(engine, clock) == 0
        local = settings.model_copy(update={"var_dir": tmp_path})
        with EventStore(engine.store.path) as reader:
            frame = read_frame(build_queries(reader, local, clock=clock), live_view(engine),
                               halt_file="HALT")  # fmt: skip
    assert [b.book_id for b in frame.summary.books] == ["A", "B", "C"]
    assert frame.summary.session is not None and frame.summary.session.state == "EXIT"
    assert not frame.summary.running  # the engine has stopped: no tasks
    assert any(d.disposition == "vetoed" and d.book_id == "B" for d in frame.decisions)
    text = text_of(frame)
    assert "stopped" in text and "EXIT" in text and "typed_veto" in text and "vetoed" in text


def test_the_cli_never_needs_the_web_extra():
    """The read model is shared, but FastAPI stays optional: the CLI must not import it."""
    code = "import sys, src.dashboard.cli; sys.exit('fastapi' in sys.modules)"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "RAKSHAQUANT_ENV_FILE": "none"}
    assert subprocess.run([sys.executable, "-c", code], cwd=root, env=env).returncode == 0


async def test_the_cli_paints_a_demo_and_leaves_its_final_state_on_screen(settings, tmp_path):
    demo = settings.model_copy(update={"environment": "demo",
                                       "state_dir": tmp_path / "var" / "demo"})  # fmt: skip
    out = io.StringIO()
    view = TerminalView(demo, console=Console(file=out, width=220), screen=False)
    assert await live.run_demo(demo, view, step_s=60.0, wall_s=0.0) == 0
    assert view.frame is not None and view.frame.summary.demo
    text = out.getvalue()
    assert text.count("RakshaQuant") == 1  # the final state, printed once (the live one cleared)
    assert "DEMO" in text and "EXIT" in text
    assert all(f" {b} " in text for b in ("A", "B", "C"))
