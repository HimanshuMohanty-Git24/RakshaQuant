"""Plan M6: the provider registry - provider:model specs, roles, fail-fast validation."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.config.errors import ConfigError
from src.config.settings import Settings
from src.llm.registry import (
    PROVIDERS,
    ModelSpec,
    ProviderKind,
    api_key,
    base_url,
    role_configs,
    validate_roles,
)

SECRET = "sk-test-not-a-real-key-123"


def test_specs_split_on_the_first_colon():
    m = ModelSpec.parse("openrouter:meta-llama/llama-3.3-70b-instruct:free")
    assert (m.provider, m.model) == ("openrouter", "meta-llama/llama-3.3-70b-instruct:free")
    assert m.is_free and str(m) == "openrouter:meta-llama/llama-3.3-70b-instruct:free"
    assert ModelSpec.parse(" anthropic:claude-sonnet-5-5 ").model == "claude-sonnet-5-5"
    for bad in ("groq", ":model", "groq:", "nope:model"):
        with pytest.raises(ConfigError):
            ModelSpec.parse(bad)


def test_the_registry_covers_the_planned_providers():
    assert set(PROVIDERS) == {"openai", "openrouter", "groq", "anthropic", "ollama", "compat"}
    assert PROVIDERS["anthropic"].kind is ProviderKind.ANTHROPIC
    assert all(
        p.kind is ProviderKind.OPENAI_COMPAT for n, p in PROVIDERS.items() if n != "anthropic"
    )
    assert PROVIDERS["groq"].base_url == "https://api.groq.com/openai/v1"
    assert PROVIDERS["openrouter"].base_url == "https://openrouter.ai/api/v1"


def test_structured_output_capabilities():
    assert ModelSpec.parse("openai:gpt-5-mini").supports_json_schema
    assert not ModelSpec.parse("groq:llama-3.3-70b-versatile").supports_json_schema
    assert ModelSpec.parse("groq:openai/gpt-oss-120b").supports_json_schema
    assert not ModelSpec.parse(
        "openrouter:meta-llama/llama-3.3-70b-instruct:free"
    ).supports_json_schema


def test_roles_come_from_settings(settings):
    s = settings.model_copy(update={
        "llm_role_veto": "openrouter:x/y:free",
        "llm_role_veto_fallbacks": "groq:llama-3.3-70b-versatile, openrouter:x/y:free",
        "llm_role_veto_effort": "low",
        "llm_role_review": "anthropic:claude-sonnet-5-5",
    })  # fmt: skip
    roles = role_configs(s)
    assert [m.spec for m in roles["veto"].chain] == [
        "openrouter:x/y:free", "groq:llama-3.3-70b-versatile",
    ]  # de-duplicated, primary first  # fmt: skip
    assert roles["veto"].effort == "low" and roles["review"].enabled
    assert not roles["explain"].enabled and not roles["label"].enabled
    with pytest.raises(ConfigError, match="EFFORT"):
        role_configs(s.model_copy(update={"llm_role_veto_effort": "extreme"}))


@pytest.mark.parametrize(
    ("spec", "key_field"),
    [("openrouter:x/y:free", "openrouter_api_key"), ("groq:llama-3.3-70b-versatile", "groq_api_key"),
     ("openai:gpt-5-mini", "openai_api_key"), ("anthropic:claude-haiku-4-5", "anthropic_api_key"),
     ("ollama:llama3.2", None)],
)  # fmt: skip
def test_swapping_the_veto_provider_is_configuration_only(settings, spec, key_field):
    update: dict[str, object] = {"llm_role_veto": spec, "groq_api_key": None}
    if key_field:
        update[key_field] = SecretStr(SECRET)
    roles = validate_roles(settings.model_copy(update=update))
    assert roles["veto"].chain[0].spec == spec


def test_a_missing_key_for_an_enabled_role_fails_fast_without_leaking(settings):
    s = settings.model_copy(update={"llm_role_veto": "openrouter:x/y:free",
                                    "llm_role_veto_fallbacks": "groq:llama-3.3-70b-versatile",
                                    "openrouter_api_key": None,
                                    "groq_api_key": SecretStr(SECRET)})  # fmt: skip
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY") as err:
        validate_roles(s)
    assert SECRET not in str(err.value) and "GROQ" not in str(err.value)
    # A disabled role needs nothing.
    assert validate_roles(settings.model_copy(update={"openrouter_api_key": None}))


def test_compat_needs_a_base_url_and_ollama_has_a_default(settings):
    with pytest.raises(ConfigError, match="LLM_COMPAT_BASE_URL"):
        validate_roles(settings.model_copy(update={"llm_role_explain": "compat:some-model"}))
    ok = settings.model_copy(update={"llm_role_explain": "compat:some-model",
                                     "llm_compat_base_url": "http://box:8000/v1"})  # fmt: skip
    assert validate_roles(ok)["explain"].enabled
    assert base_url(PROVIDERS["ollama"], settings) == "http://localhost:11434/v1"
    assert api_key(PROVIDERS["ollama"], settings) is None


def test_groq_is_no_longer_required(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.groq_api_key is None and s.llm_role_veto == ""
