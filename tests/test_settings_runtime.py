"""
Plan M0.4: environment + absolute state directory, repo-anchored .env, opt-in LangSmith,
and secret-typed Telegram token / database URL.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr, ValidationError

from src.config import settings as settings_module
from src.config.settings import REPO_ROOT, Settings
from src.execution.costs import CostModel
from src.execution.paper_engine import LocalPaperEngine
from src.observability.tracing import setup_tracing


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


def test_langsmith_is_optional_and_off_by_default(monkeypatch):
    s = _make(monkeypatch)
    assert s.langsmith_api_key is None
    assert s.langsmith_tracing_v2 is False


def test_tracing_on_without_key_warns(monkeypatch):
    s = _make(monkeypatch, langsmith_tracing_v2=True)
    assert any("LANGSMITH_API_KEY" in w for w in s.config_warnings)


def test_setup_tracing_is_a_no_op_by_default(settings):
    with (
        patch("src.observability.tracing.get_settings", return_value=settings),
        patch("src.observability.tracing.Client") as client,
    ):
        assert setup_tracing() is False
    client.assert_not_called()
    assert "LANGSMITH_API_KEY" not in os.environ
    assert "LANGSMITH_TRACING_V2" not in os.environ


def test_setup_tracing_exports_nothing_when_key_is_rejected(monkeypatch):
    s = _make(monkeypatch, langsmith_tracing_v2=True, langsmith_api_key="bad")
    with (
        patch("src.observability.tracing.get_settings", return_value=s),
        patch("src.observability.tracing.Client") as client,
    ):
        client.return_value.list_projects.side_effect = RuntimeError("401")
        assert setup_tracing() is False
    assert "LANGSMITH_API_KEY" not in os.environ


def test_telegram_token_and_database_url_are_secret(monkeypatch):
    s = _make(
        monkeypatch,
        telegram_bot_token="bot123:SECRETTOKEN",
        telegram_chat_id="42",
        database_url="postgresql://u:hunter2@h/db",
    )
    assert isinstance(s.telegram_bot_token, SecretStr)
    assert isinstance(s.database_url, SecretStr)
    assert "SECRETTOKEN" not in repr(s) and "hunter2" not in repr(s)
    assert s.database_url.get_secret_value() == "postgresql://u:hunter2@h/db"


def test_legacy_paper_wallet_lives_under_state_dir(settings):
    with patch("src.execution.paper_engine.get_settings", return_value=settings):
        engine = LocalPaperEngine(cost_model=CostModel.zero())
    assert engine.state_file == settings.state_dir / "paper_wallet.json"

    engine.place_order("INFY", "BUY", 1, 100.0)
    assert engine.state_file.exists()  # parent directory created on first write


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
