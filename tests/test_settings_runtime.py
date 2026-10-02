"""
Plan M0.4: environment + absolute state directory, repo-anchored .env and a secret-typed
Telegram token. Plan M12.1: the legacy settings are gone, and an old .env still loads.
"""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from src.config import settings as settings_module
from src.config.settings import REPO_ROOT, Settings


def _make(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    for name in ("VAR_DIR", "STATE_DIR", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, groq_api_key="x", **overrides)


def test_repo_root_and_env_file_are_absolute_and_anchored():
    assert REPO_ROOT == Path(__file__).resolve().parents[1]
    assert settings_module.ENV_FILE == REPO_ROOT / ".env"
    assert settings_module.ENV_FILE.is_absolute()


def test_default_environment_is_dev_with_state_under_repo_var(monkeypatch):
    s = _make(monkeypatch)
    assert s.environment == "dev"
    assert s.state_dir == REPO_ROOT / "var" / "dev"
    assert s.state_dir.is_absolute()


@pytest.mark.parametrize("env", ["dev", "paper", "demo", "test"])
def test_state_dir_follows_environment(monkeypatch, env):
    assert _make(monkeypatch, environment=env).state_dir == REPO_ROOT / "var" / env


def test_unknown_environment_is_rejected(monkeypatch):
    with pytest.raises(ValidationError):
        _make(monkeypatch, environment="prod")


def test_relative_state_dir_resolves_against_repo_not_cwd(monkeypatch, tmp_path):
    assert Path.cwd() == tmp_path  # conftest chdir
    s = _make(monkeypatch, state_dir="custom/state")
    assert s.state_dir == REPO_ROOT / "custom" / "state"


def test_absolute_state_dir_is_kept(monkeypatch, tmp_path):
    assert _make(monkeypatch, state_dir=tmp_path / "s").state_dir == tmp_path / "s"


def test_settings_construction_does_not_create_state_dir(monkeypatch, tmp_path):
    target = tmp_path / "not-yet"
    _make(monkeypatch, state_dir=target)
    assert not target.exists()


def test_telegram_token_is_secret(monkeypatch):
    s = _make(monkeypatch, telegram_bot_token="bot123:SECRETTOKEN", telegram_chat_id="42")
    assert isinstance(s.telegram_bot_token, SecretStr)
    assert "SECRETTOKEN" not in repr(s)


def test_an_env_file_with_retired_settings_still_loads(monkeypatch, tmp_path):
    """Plan M12.1 removed the LangChain, Postgres, Redis, FinOps and goal-engine settings; a .env
    written for the old stack must still load (unknown keys are ignored), not fail startup."""
    env = tmp_path / "old.env"
    env.write_text("LANGSMITH_TRACING_V2=true\nLANGSMITH_API_KEY=ls-x\nDATABASE_URL=postgres://u:p@h/d\n"
                   "REDIS_URL=redis://h\nGROQ_MODEL_PRIMARY=m\nDAILY_LOSS_LIMIT=1\n"
                   "MONTHLY_PROFIT_TARGET_PCT=0.05\nENABLE_LEARNING=false\n", encoding="utf-8")  # fmt: skip
    for name in ("VAR_DIR", "STATE_DIR", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=env)
    for retired in ("langsmith_api_key", "database_url", "redis_url", "groq_model_primary",
                    "daily_loss_limit", "monthly_profit_target_pct", "enable_learning"):  # fmt: skip
        assert not hasattr(s, retired), retired
    assert s.config_warnings == []


def test_var_dir_layout_follows_the_plan(monkeypatch, tmp_path):
    s = _make(monkeypatch, environment="paper")
    assert s.var_dir == REPO_ROOT / "var"
    assert s.db_path == REPO_ROOT / "var" / "paper" / "rakshaquant.db"
    assert s.tape_dir == REPO_ROOT / "var" / "tape"
    assert {s.logs_dir.name, s.reports_dir.name, s.reference_dir.name} == {
        "logs",
        "reports",
        "reference",
    }
    moved = _make(monkeypatch, var_dir=tmp_path / "v", environment="demo")
    assert moved.state_dir == tmp_path / "v" / "demo"
    assert moved.logs_dir == tmp_path / "v" / "logs"


def test_conftest_isolates_var_dir(tmp_path):
    from src.config import get_settings

    s = get_settings()
    assert s.var_dir == tmp_path / "var" and s.state_dir == tmp_path / "var" / "test"
