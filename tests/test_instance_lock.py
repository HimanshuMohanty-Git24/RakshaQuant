"""Plan M0.5: one RakshaQuant process per state directory; a second one exits with code 3."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from src.config import get_settings
from src.ops.exit_codes import ExitCode
from src.ops.instance_lock import LOCK_FILE_NAME, InstanceLockHeldError, single_instance

REPO_ROOT = Path(__file__).resolve().parents[1]

# Child process: try to take the lock, exit 3 if it is held. Imports no settings, so it never
# reads the real .env.
_CHILD = """
import sys
from pathlib import Path
from src.ops.instance_lock import InstanceLockHeldError, single_instance
try:
    with single_instance(Path(sys.argv[1])):
        pass
except InstanceLockHeldError:
    sys.exit(3)
"""


def _child(state_dir: Path) -> int:
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(state_dir)], cwd=REPO_ROOT, timeout=60)
    return proc.returncode


def test_lock_file_lives_in_state_dir(tmp_path):
    state_dir = tmp_path / "var" / "paper"
    with single_instance(state_dir) as lock_path:
        assert lock_path == state_dir / LOCK_FILE_NAME
        assert lock_path.exists()


def test_second_holder_is_refused_until_release(tmp_path):
    with single_instance(tmp_path):
        with pytest.raises(InstanceLockHeldError, match="already running"):
            with single_instance(tmp_path):
                pass
    with single_instance(tmp_path):  # released: free again
        pass


def test_other_environments_do_not_block_each_other(tmp_path):
    with single_instance(tmp_path / "paper"), single_instance(tmp_path / "dev"):
        pass


def test_lock_excludes_another_process(tmp_path):
    with single_instance(tmp_path):
        assert _child(tmp_path) == ExitCode.LOCK_HELD
    assert _child(tmp_path) == ExitCode.OK


def _load_entry_point() -> ModuleType:
    path = REPO_ROOT / "scripts" / "run_live_trading.py"
    spec = importlib.util.spec_from_file_location("run_live_trading_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_point_exits_3_when_another_instance_runs(monkeypatch):
    entry = _load_entry_point()

    async def _must_not_run() -> None:
        raise AssertionError("trading loop started despite a held lock")

    monkeypatch.setattr(entry, "_run_cli", _must_not_run)
    monkeypatch.setattr(sys, "argv", ["run_live_trading.py"])

    with single_instance(get_settings().state_dir):
        with pytest.raises(SystemExit) as exc:
            entry.main()

    assert exc.value.code == ExitCode.LOCK_HELD
