"""Plan M12.4: the scheduled session's pieces - the dead-man check, the code version in every
``ProcessStarted``, the console that exits after its session, and the Task Scheduler script."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

import src
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.events import Heartbeat
from src.engine.tasks import HEARTBEAT_INTERVAL_S
from src.ops import version
from src.ops.deadman import STALE_AFTER_S, DeadmanResult, alarm_text, check
from src.ops.exit_codes import ExitCode
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.utils.market_time import IST
from src.web.run_manager import RunManager
from src.web.server import create_app
from tests.web_helpers import SECURITY

ROOT = Path(__file__).resolve().parents[1]
MONDAY = datetime(2026, 10, 5, 10, 0, tzinfo=IST)  # a trading day, 10:00 IST


def beats(path: Path, *instants: datetime) -> None:
    with EventStore(path) as store:
        for at in instants:
            StoreSink(store, ReplayClock(at), "engine").emit(Heartbeat(pid=1, uptime_s=1.0))


# -- the dead-man check -------------------------------------------------------------------------


def test_a_fresh_heartbeat_is_ok_and_a_stale_or_missing_one_is_an_alarm(tmp_path):
    db, cal = tmp_path / "rq.db", get_calendar()
    beats(db, MONDAY - timedelta(minutes=10), MONDAY - timedelta(seconds=40))
    ok = check(db, now=MONDAY, calendar=cal)
    assert ok.status == "ok" and not ok.alarm and ok.last_beat is not None
    late = check(db, now=MONDAY + timedelta(minutes=5), calendar=cal)
    assert late.alarm and "no heartbeat for 6 min" in late.detail and "09:59:20 IST" in late.detail
    assert check(tmp_path / "none.db", now=MONDAY, calendar=cal).alarm  # no session ever ran
    tomorrow = MONDAY + timedelta(days=1)
    assert "no heartbeat today" in check(db, now=tomorrow, calendar=cal).detail
    assert not (tmp_path / "none.db").exists()  # the check never creates a store


def test_closed_markets_are_skipped_and_an_uncovered_year_is_an_alarm(tmp_path):
    db, cal = tmp_path / "rq.db", get_calendar()
    for closed in (datetime(2026, 10, 2, 10, 0, tzinfo=IST),  # Gandhi Jayanti
                   datetime(2026, 10, 3, 10, 0, tzinfo=IST),  # Saturday
                   datetime(2026, 10, 5, 8, 30, tzinfo=IST),  # before the open
                   datetime(2026, 10, 5, 15, 45, tzinfo=IST)):  # after the close  # fmt: skip
        assert check(db, now=closed, calendar=cal).status == "skipped"
    uncovered = check(db, now=datetime(2027, 1, 4, 10, 0, tzinfo=IST), calendar=cal)
    assert uncovered.alarm and "calendar does not cover 2027-01-04" in uncovered.detail


def test_the_alarm_threshold_spans_several_heartbeats():
    assert STALE_AFTER_S >= 6 * HEARTBEAT_INTERVAL_S == 180.0
    text = alarm_text(DeadmanResult("stale", "no heartbeat for 7 min"), "paper")
    assert "(paper)" in text and "no heartbeat for 7 min" in text and "incident.md" in text


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("enabled", "sent", "code"), [
    (False, False, ExitCode.CONFIG_ERROR), (True, True, ExitCode.OK), (True, False, ExitCode.CRASH),
])  # fmt: skip
def test_the_script_alarms_through_telegram(monkeypatch, capsys, enabled, sent, code):
    script = load_script("deadman_check")
    messages: list[str] = []

    class FakeNotifier:
        def __init__(self) -> None:
            self.enabled = enabled

        async def send_message(self, text: str) -> bool:
            messages.append(text)
            return sent

    monkeypatch.setattr(script, "check", lambda *a, **k: DeadmanResult("stale", "no heartbeat"))
    monkeypatch.setattr(script, "TelegramNotifier", FakeNotifier)
    assert script.main() == code
    assert len(messages) == (1 if enabled else 0)
    assert "Telegram is not configured" in capsys.readouterr().out or enabled


def test_a_healthy_check_sends_nothing(monkeypatch):
    script = load_script("deadman_check")
    monkeypatch.setattr(script, "check", lambda *a, **k: DeadmanResult("ok", "fine"))
    monkeypatch.setattr(script, "TelegramNotifier", lambda: pytest.fail("no alarm expected"))
    assert script.main() == ExitCode.OK


# -- the code version ---------------------------------------------------------------------------


def test_the_code_version_names_the_commit(monkeypatch):
    version.code_version.cache_clear()
    try:
        found = version.code_version()
        assert found.startswith(src.__version__)
        if shutil.which("git") and (ROOT / ".git").exists():
            commit = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT,
                                    capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
            assert found.startswith(f"{src.__version__}+g{commit}")
        version.code_version.cache_clear()
        monkeypatch.setattr(version, "_git", lambda *args: None)  # no git on the machine
        assert version.code_version() == src.__version__
    finally:
        version.code_version.cache_clear()


# -- the console that exits after its session ---------------------------------------------------


class QuickRun(RunManager):
    """A run that ends at once, instead of a trading day."""

    async def start(self, *, demo: bool = False) -> dict[str, Any]:
        self._task = asyncio.create_task(asyncio.sleep(0.05))
        return {"running": True, "demo": demo}


def test_the_console_shuts_down_once_its_session_has_ended(settings):
    ended = threading.Event()
    app = create_app(manager=QuickRun(settings), security=SECURITY, auto_start_demo=False,
                     on_session_end=ended.set)  # fmt: skip
    with TestClient(app):
        assert ended.wait(5)


def test_without_the_flag_the_console_keeps_serving(settings):
    app = create_app(manager=QuickRun(settings), security=SECURITY, auto_start_demo=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_the_flag_needs_an_auto_started_web_session():
    script = load_script("run_live_trading")
    assert script._parse_args(["--mode", "web", "--exit-after-session"]).exit_after_session
    for bad in (["--exit-after-session"], ["--mode", "web", "--no-auto-start",
                                           "--exit-after-session"]):  # fmt: skip
        with pytest.raises(SystemExit):
            script._parse_args(bad)


# -- the Task Scheduler script ------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler")
def test_the_installer_dry_run_registers_nothing(tmp_path):
    """Against a stand-in repository (never the real .env): it plans both tasks at IST times and
    registers nothing."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_live_trading.py").write_text("", encoding="utf-8")
    done = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "scripts" / "install_windows_task.ps1"), "-DryRun",
         "-RepoRoot", str(tmp_path), "-Python", sys.executable],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "09:05 IST" in out and "10:00 IST" in out and "13:00 IST" in out
    assert "--mode web --exit-after-session" in out and "deadman_check.py" in out
    assert "dry run: nothing registered" in out and "registered:" not in out
    assert ".env not found" in done.stdout + done.stderr


def test_now_is_never_read_from_the_wall_clock_in_the_check(tmp_path):
    """The check takes ``now``: the same store gives the same answer at the same instant."""
    db = tmp_path / "rq.db"
    beats(db, MONDAY - timedelta(seconds=30))
    first = check(db, now=MONDAY, calendar=get_calendar())
    assert first == check(db, now=MONDAY, calendar=get_calendar())
    assert first.last_beat == (MONDAY - timedelta(seconds=30)).astimezone(UTC)
