"""LLM clients (plan M6): one per provider kind, created lazily and cached per provider.

The provider SDKs are imported only when a client is first built: ``anthropic`` alone takes
about 1.7 s to import, which every process would otherwise pay with no LLM role enabled.
"""

from __future__ import annotations

from typing import Any

from src.llm.clients.base import LLMClient
from src.llm.registry import PROVIDERS, ProviderKind, api_key, base_url


class ClientFactory:
    """One cached client per provider, built from the settings on first use."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._clients: dict[str, LLMClient] = {}

    def get(self, provider: str) -> LLMClient:
        client = self._clients.get(provider)
        if client is None:
            client = self._build(provider)
            self._clients[provider] = client
        return client

    def _build(self, provider: str) -> LLMClient:
        spec = PROVIDERS[provider]
        s = self._settings
        timeout = float(s.llm_timeout_s)
        if spec.kind is ProviderKind.ANTHROPIC:
            from src.llm.clients.anthropic_native import AnthropicNativeClient

            return AnthropicNativeClient(api_key=api_key(spec, s), timeout_s=timeout,
                                         fallbacks=bool(s.llm_anthropic_fallbacks))  # fmt: skip
        from src.llm.clients.openai_compat import OpenAICompatClient

        return OpenAICompatClient(
            spec, api_key=api_key(spec, s), base_url=base_url(spec, s), timeout_s=timeout,
            referer=str(s.llm_openrouter_referer or ""),
            deny_data_collection=bool(s.llm_openrouter_deny_data_collection),
        )  # fmt: skip


__all__ = ["ClientFactory", "LLMClient"]
