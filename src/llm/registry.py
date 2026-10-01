"""
The LLM provider registry (plan M6; decision D6): any role on any provider by changing one config
string, ``"provider:model"``. No code changes, no blocking dependency on one vendor.

* :data:`PROVIDERS` - one :class:`ProviderSpec` per provider (another OpenAI-compatible vendor is
  one more line here).
* :meth:`ModelSpec.parse` splits on the **first** colon, so ``openrouter:vendor/model:free`` works.
* :func:`role_configs` reads ``LLM_ROLE_<ROLE>`` (primary), ``LLM_ROLE_<ROLE>_FALLBACKS``
  (comma-separated) and ``LLM_ROLE_<ROLE>_EFFORT``; an empty primary disables the role.
* :func:`validate_roles` fails fast (``ConfigError`` → exit 2) when an **enabled** role names an
  unknown provider or a provider whose key is missing. Messages name the env var, never a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import SecretStr

from src.config.errors import ConfigError

ROLES = ("veto", "review", "explain", "label", "research")
EFFORTS = frozenset({"low", "medium", "high", "max"})


class ProviderKind(StrEnum):
    OPENAI_COMPAT = "openai_compat"
    ANTHROPIC = "anthropic"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: ProviderKind
    base_url: str | None  # None = the SDK's default
    api_key_env: str | None  # for messages only
    key_setting: str | None  # the Settings attribute holding the key
    key_optional: bool = False
    base_url_setting: str | None = None  # a Settings attribute that overrides base_url
    default_headers: Mapping[str, str] = field(default_factory=dict)
    supports_json_schema: bool = False
    supports_json_mode: bool = True


PROVIDERS: Mapping[str, ProviderSpec] = {
    p.name: p
    for p in (
        ProviderSpec("openai", ProviderKind.OPENAI_COMPAT, None, "OPENAI_API_KEY",
                     "openai_api_key", supports_json_schema=True),
        ProviderSpec("openrouter", ProviderKind.OPENAI_COMPAT, "https://openrouter.ai/api/v1",
                     "OPENROUTER_API_KEY", "openrouter_api_key",
                     default_headers={"X-Title": "RakshaQuant"}),
        ProviderSpec("groq", ProviderKind.OPENAI_COMPAT, "https://api.groq.com/openai/v1",
                     "GROQ_API_KEY", "groq_api_key"),
        ProviderSpec("anthropic", ProviderKind.ANTHROPIC, None, "ANTHROPIC_API_KEY",
                     "anthropic_api_key", supports_json_schema=True, supports_json_mode=False),
        ProviderSpec("ollama", ProviderKind.OPENAI_COMPAT, "http://localhost:11434/v1", None,
                     None, key_optional=True, base_url_setting="ollama_base_url"),
        ProviderSpec("compat", ProviderKind.OPENAI_COMPAT, None, "LLM_COMPAT_API_KEY",
                     "llm_compat_api_key", key_optional=True,
                     base_url_setting="llm_compat_base_url"),
    )
}  # fmt: skip

# Per-model structured-output capability overrides ("provider:model-prefix" -> json_schema).
# Providers default to their ProviderSpec; these models differ from their provider's default.
JSON_SCHEMA_OVERRIDES: Mapping[str, bool] = {
    "groq:openai/gpt-oss": True,  # Groq's strict structured outputs (verify per model)
    "groq:moonshotai/kimi-k2": True,
    "openrouter:openai/": True,
    "openrouter:anthropic/": True,
    "openrouter:google/gemini": True,
}


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    @property
    def spec(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def provider_spec(self) -> ProviderSpec:
        return PROVIDERS[self.provider]

    @property
    def is_free(self) -> bool:
        return self.model.endswith(":free")

    @property
    def supports_json_schema(self) -> bool:
        for prefix, supported in JSON_SCHEMA_OVERRIDES.items():
            if self.spec.startswith(prefix):
                return supported
        return self.provider_spec.supports_json_schema

    @classmethod
    def parse(cls, text: str) -> ModelSpec:
        """``"provider:model"``, split on the first colon (model ids may contain colons)."""
        provider, sep, model = text.strip().partition(":")
        if not sep or not provider or not model:
            raise ConfigError(f"model spec {text!r} must look like 'provider:model'")
        if provider not in PROVIDERS:
            raise ConfigError(
                f"unknown LLM provider {provider!r} in {text!r} (known: {', '.join(PROVIDERS)})"
            )
        return cls(provider, model)

    def __str__(self) -> str:
        return self.spec


@dataclass(frozen=True)
class RoleConfig:
    role: str
    chain: tuple[ModelSpec, ...] = ()  # primary first
    effort: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.chain)


def role_configs(settings: Any) -> dict[str, RoleConfig]:
    """Every role from the settings (disabled roles have an empty chain)."""
    out: dict[str, RoleConfig] = {}
    for role in ROLES:
        primary = str(getattr(settings, f"llm_role_{role}", "") or "").strip()
        fallbacks = str(getattr(settings, f"llm_role_{role}_fallbacks", "") or "")
        effort = getattr(settings, f"llm_role_{role}_effort", None) or None
        if effort is not None and effort not in EFFORTS:
            raise ConfigError(
                f"LLM_ROLE_{role.upper()}_EFFORT={effort!r}: use one of {sorted(EFFORTS)}"
            )
        if not primary:
            out[role] = RoleConfig(role, (), effort)
            continue
        specs = [primary, *(f.strip() for f in fallbacks.split(",") if f.strip())]
        chain = tuple(dict.fromkeys(ModelSpec.parse(s) for s in specs))
        out[role] = RoleConfig(role, chain, effort)
    return out


def api_key(provider: ProviderSpec, settings: Any) -> str | None:
    """The provider's key from the settings (``None`` when unset). Never log the result."""
    if provider.key_setting is None:
        return None
    value = getattr(settings, provider.key_setting, None)
    if isinstance(value, SecretStr):
        secret = value.get_secret_value()
        return secret or None
    return str(value) if value else None


def base_url(provider: ProviderSpec, settings: Any) -> str | None:
    if provider.base_url_setting is not None:
        configured = getattr(settings, provider.base_url_setting, None)
        if configured:
            return str(configured)
    return provider.base_url


def validate_roles(settings: Any) -> dict[str, RoleConfig]:
    """Parse and check every role; raise one ``ConfigError`` listing every problem."""
    roles = role_configs(settings)
    problems: list[str] = []
    for role in roles.values():
        for model in role.chain:
            provider = model.provider_spec
            if not provider.key_optional and api_key(provider, settings) is None:
                problems.append(
                    f"role {role.role}: {model.spec} needs {provider.api_key_env} (not set)"
                )
            if provider.base_url_setting and not base_url(provider, settings):
                problems.append(
                    f"role {role.role}: {model.spec} needs {provider.base_url_setting.upper()}"
                )
    if problems:
        raise ConfigError("LLM configuration: " + "; ".join(problems))
    return roles
