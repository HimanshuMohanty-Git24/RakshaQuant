"""
Hermetic test harness (plan M0.3, audit RQ-01).

The suite must never touch real state or real credentials:

* every test runs with the working directory set to its own ``tmp_path``, and with
  ``ENVIRONMENT=test`` and ``VAR_DIR`` under that ``tmp_path``, so runtime state (the event
  store, tape, logs, ``paper_wallet.json``, ...) never lands in the repo;
* ``.env`` is never read: ``Settings.model_config["env_file"]`` is disabled for the whole
  session (including collection-time imports), and the cached ``get_settings()`` is cleared
  around every test;
* provider/broker secrets, and every env var that maps to a ``Settings`` field, are removed
  from the environment; required keys get obvious placeholders;
* ``os.environ`` is restored after every test, so code that writes to it directly (e.g.
  ``setup_tracing``) cannot leak into the next test.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config.settings import Settings, get_settings

# Secrets and connection strings. Prefix entries end with "_".
SECRET_ENV_KEYS = (
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TYPESAFE_API_KEY",
    "LLM_COMPAT_API_KEY",
    "DATABASE_URL",
    "DHAN_",
    "LANGSMITH_",
    "TELEGRAM_",
)

# Required settings get placeholders that can never be mistaken for real keys. The in-memory
# DATABASE_URL stops tests reaching a Postgres that happens to run at the default URL.
PLACEHOLDER_ENV = {
    "ENVIRONMENT": "test",
    "GROQ_API_KEY": "test-groq-key",
    "DATABASE_URL": "sqlite:///:memory:",
}


_SETTINGS_ENV_NAMES = frozenset(name.upper() for name in Settings.model_fields)


def _is_scrubbed(name: str) -> bool:
    upper = name.upper()
    if upper in _SETTINGS_ENV_NAMES:
        return True
    return any(
        upper.startswith(key) if key.endswith("_") else upper == key for key in SECRET_ENV_KEYS
    )


def _scrub_environ(var_dir: Path) -> None:
    for name in [n for n in os.environ if _is_scrubbed(n)]:
        del os.environ[name]
    os.environ.update(PLACEHOLDER_ENV)
    os.environ["VAR_DIR"] = str(var_dir)


def pytest_configure(config: pytest.Config) -> None:
    """Session-wide guard, active before test modules are imported."""
    Settings.model_config["env_file"] = None
    _scrub_environ(Path(tempfile.mkdtemp(prefix="rq-test-var-")))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ):
        _scrub_environ(tmp_path / "var")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A fresh ``Settings`` built from code defaults only (no ``.env``, no real keys)."""
    return Settings(
        _env_file=None,
        var_dir=tmp_path / "var",
        **{k.lower(): v for k, v in PLACEHOLDER_ENV.items()},
    )
