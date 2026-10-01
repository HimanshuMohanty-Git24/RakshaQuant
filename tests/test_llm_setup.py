"""Plan M6: roles from settings, the router built at startup, scripts/llm_check.py."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

import src.llm.setup as setup
from src.config.errors import ConfigError
from src.domain.clock import ReplayClock
from src.engine.live import run_paper
from src.llm.types import ClientReply, LLMServerError, Message, Usage
from src.store.event_store import EventStore
from src.store.sink import StoreSink

SECRET = "sk-or-test-secret-value-42"
ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeFactory:
    def __init__(self, settings: Any = None, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()

    def get(self, provider: str) -> Any:
        fail = provider in self.fail

        class Client:
            async def complete(self, model: Any, messages: Any, schema: type[BaseModel], **kw: Any):
                if fail:
                    raise LLMServerError("down")
                return ClientReply(parsed=schema.model_validate({"ok": True, "schema_version": 1}),
                                   usage=Usage(12, 5))  # fmt: skip

        return Client()


def configured(settings: Any, **update: Any) -> Any:
    base = {"llm_role_veto": "openrouter:x/y:free", "openrouter_api_key": SecretStr(SECRET),
            "llm_role_review": "anthropic:claude-sonnet-5-5",
            "anthropic_api_key": SecretStr(SECRET)}  # fmt: skip
    base.update(update)
    return settings.model_copy(update=base)


def test_the_router_is_built_from_settings_with_todays_spend(settings, tmp_path):
    s = configured(settings)
    clock = ReplayClock(T0)
    with EventStore(tmp_path / "rq.db") as store:
        sink = StoreSink(store, clock, "llm")
        first = setup.build_router(s, clock=clock, sink=sink, store=store, clients=FakeFactory())
        assert set(r for r, c in first.roles.items() if c.enabled) == {"veto", "review"}

        class Out(BaseModel):
            ok: bool
            schema_version: int

        asyncio.run(first.complete("review", [Message("user", "hi")], Out, prompt_version="p"))
        again = setup.build_router(s, clock=clock, sink=sink, store=store, clients=FakeFactory())
        assert again.ledger.total == first.ledger.total > 0  # rebuilt from the LLMCall events


def test_a_misconfigured_enabled_role_fails_startup_before_any_network(settings):
    bad = configured(settings, llm_role_veto="nope:model")
    with pytest.raises(ConfigError, match="unknown LLM provider"):
        asyncio.run(run_paper(bad.model_copy(update={"environment": "paper"}), None))  # type: ignore[arg-type]


def test_llm_check_pings_each_enabled_role_and_never_prints_keys(settings, monkeypatch, capsys):
    script = load_script("llm_check")
    s = configured(settings)
    monkeypatch.setattr(script, "get_settings", lambda: s)
    monkeypatch.setattr(setup, "ClientFactory", lambda _s: FakeFactory(fail={"anthropic"}))
    code = asyncio.run(script.check())
    out = capsys.readouterr().out
    assert code == 1  # review failed
    lines = sorted(out.strip().splitlines())
    assert lines[0].startswith("review | anthropic:claude-sonnet-5-5 | fail (server_error)")
    assert lines[1].startswith("veto | openrouter:x/y:free | ok |") and lines[1].endswith("12/5")
    assert SECRET not in out
    with EventStore(s.db_path) as store:  # the pings were recorded as LLMCall events
        assert len(store.read(types=["LLMCall"])) == 2


def test_llm_check_with_no_roles(settings, monkeypatch, capsys):
    script = load_script("llm_check")
    monkeypatch.setattr(script, "get_settings", lambda: settings)
    assert asyncio.run(script.check()) == 0
    assert "No LLM roles are enabled" in capsys.readouterr().out


def test_check_config_reports_roles_and_fails_on_a_missing_key(settings, monkeypatch, capsys):
    script = load_script("check_config")
    monkeypatch.setattr(script, "get_settings", lambda: configured(settings))
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "LLM role veto" in out and "openrouter:x/y:free" in out and SECRET not in out
    missing = configured(settings, openrouter_api_key=None)
    monkeypatch.setattr(script, "get_settings", lambda: missing)
    assert script.main() == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().out


def test_importing_the_script_calls_no_provider(monkeypatch):
    monkeypatch.setattr(setup, "ClientFactory", lambda _s: pytest.fail("built a client"))
    assert load_script("llm_check").PING.prompt_version.startswith("ping_v1@")
