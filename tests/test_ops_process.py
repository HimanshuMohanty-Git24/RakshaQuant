"""Plan M1.6: the entry-point runner's exit codes, process events and crash handling."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import get_settings
from src.ops.exit_codes import ExitCode
from src.ops.instance_lock import single_instance
from src.ops.process import ConfigError, run
from src.store.event_store import EventStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _raise(exc: BaseException):
    def main() -> int:
        raise exc

    return main


@pytest.mark.parametrize(
    ("main", "code"),
    [
        (lambda: None, ExitCode.OK),
        (lambda: 0, ExitCode.OK),
        (lambda: 5, 5),
        (_raise(KeyboardInterrupt()), ExitCode.OK),
        (_raise(SystemExit()), ExitCode.OK),
        (_raise(SystemExit(4)), 4),
        (_raise(ConfigError("bad limit")), ExitCode.CONFIG_ERROR),
        (_raise(RuntimeError("boom")), ExitCode.CRASH),
    ],
    ids=["none", "zero", "five", "ctrl-c", "sysexit-none", "sysexit-4", "config", "crash"],
)
def test_exit_codes(main, code):
    assert run("probe", main) == code


def test_crash_is_logged_with_traceback_and_redacted(capsys):
    def main() -> int:
        raise RuntimeError("upstream said token=SECRETVALUE")

    assert run("probe", main) == ExitCode.CRASH
    err = capsys.readouterr().err
    assert "probe crashed: RuntimeError" in err and "SECRETVALUE" not in err
    logs = get_settings().logs_dir
    records = [json.loads(line) for p in logs.glob("*.log") for line in p.read_text().splitlines()]
    crash = next(r for r in records if r["msg"] == "probe crashed")
    assert crash["level"] == "ERROR" and "Traceback" in crash["exc"]
    assert "SECRETVALUE" not in json.dumps(records)


def test_process_events_are_recorded(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["probe.py", "--mode", "cli"])
    assert run("probe", lambda: 0, record_events=True) == 0
    assert run("probe", _raise(RuntimeError("x")), record_events=True) == ExitCode.CRASH
    with EventStore(get_settings().db_path) as store:
        events = store.read(types=["ProcessStarted", "ProcessStopped"])
    kinds = [(e.type, getattr(e.payload, "reason", None)) for e in events]
    assert kinds == [
        ("ProcessStarted", None),
        ("ProcessStopped", "normal"),
        ("ProcessStarted", None),
        ("ProcessStopped", "crash"),
    ]
    started = events[0].payload
    assert started.argv == ("--mode", "cli") and started.environment == "test"  # type: ignore[attr-defined]
    assert events[3].payload.exit_code == 1  # type: ignore[attr-defined]


def test_lock_held_exits_3_without_running_main():
    ran: list[bool] = []
    with single_instance(get_settings().state_dir):
        code = run("probe", lambda: ran.append(True), lock=True)
    assert code == ExitCode.LOCK_HELD and ran == []


def test_config_error_never_echoes_input_values(monkeypatch, capsys):
    monkeypatch.setenv("ENVIRONMENT", "sk-ant-api03-SECRETVALUE")
    assert run("probe", lambda: 0) == ExitCode.CONFIG_ERROR
    err = capsys.readouterr().err
    assert "configuration error" in err and "environment" in err
    assert "SECRETVALUE" not in err


# --- real processes ---------------------------------------------------------------------------

_CHILD = """
import sys
from src.ops.process import run_entry_point
def main():
    if sys.argv[1] == "crash":
        raise RuntimeError("child crashed")
    return 0
run_entry_point("child", main)
"""


def _child(arg: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CHILD, arg],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_crashing_process_exits_1():
    proc = _child("crash")
    assert proc.returncode == ExitCode.CRASH, proc.stderr
    assert "child crashed" in proc.stderr


def test_a_normal_process_exits_0():
    proc = _child("ok")
    assert proc.returncode == ExitCode.OK, proc.stderr


def test_a_misconfigured_process_exits_2():
    proc = _child("ok", ENVIRONMENT="production")
    assert proc.returncode == ExitCode.CONFIG_ERROR, proc.stderr
    assert "environment" in proc.stderr
