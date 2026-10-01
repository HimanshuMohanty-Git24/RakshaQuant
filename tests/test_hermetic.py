"""Guards for the hermetic harness in conftest.py (plan M0.3)."""

import os
from pathlib import Path

from src.config.settings import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cwd_is_isolated_from_repo(tmp_path):
    assert Path.cwd() == tmp_path
    assert Path.cwd().resolve() != REPO_ROOT


def test_env_file_is_never_read():
    assert Settings.model_config.get("env_file") is None


def test_settings_carry_no_real_credentials():
    s = get_settings()
    assert s.groq_api_key.get_secret_value() == "test-groq-key"
    assert s.dhan_client_id is None
    assert s.dhan_access_token is None
    assert not any(k.startswith(("DHAN_", "TELEGRAM_")) for k in os.environ)


# A name the scrubber ignores, so part 2 proves os.environ is restored (runs in file order).
_PROBE = "RQ_HERMETIC_LEAK_PROBE"


def test_environ_writes_do_not_leak_part1():
    os.environ[_PROBE] = "leak"


def test_environ_writes_do_not_leak_part2():
    assert _PROBE not in os.environ
