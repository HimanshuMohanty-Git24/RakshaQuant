"""
Every OpenAI-compatible provider (plan M6): OpenAI, OpenRouter, Groq, Ollama, any ``compat`` URL.

* One cached ``AsyncOpenAI(base_url, api_key, timeout, max_retries=0)`` per provider - the router
  owns retries and fallbacks.
* Structured output: ``response_format`` ``json_schema`` (strict) when the model supports it,
  else ``json_object`` with the schema in the system prompt. Always validated with Pydantic.
* OpenRouter (verified against its docs 2026-10-02): ``X-OpenRouter-Title`` (+ optional
  ``HTTP-Referer``); usage, including ``usage.cost`` (credits = USD), is always returned (the old
  ``usage.include`` flag is deprecated); ``provider.data_collection = "deny"`` for prompts with
  private context. A model without structured-output support ignores ``response_format``, so
  every reply is validated with Pydantic regardless.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import openai
from pydantic import BaseModel

from src.llm.clients.base import parse_json, schema_instruction, strict_schema
from src.llm.registry import ModelSpec, ProviderSpec
from src.llm.types import (
    ClientReply,
    LLMError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMServerError,
    LLMTimeoutError,
    Message,
    Usage,
)


class OpenAICompatClient:
    def __init__(
        self,
        provider: ProviderSpec,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout_s: float,
        referer: str = "",
        deny_data_collection: bool = True,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        self.provider = provider
        headers = dict(provider.default_headers)
        if provider.name == "openrouter" and referer:
            headers["HTTP-Referer"] = referer
        self._deny = deny_data_collection
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or "not-needed",  # e.g. Ollama ignores it; the SDK requires one
            base_url=base_url,
            timeout=timeout_s,
            max_retries=0,
            default_headers=headers or None,
        )

    async def complete(
        self,
        model: ModelSpec,
        messages: Sequence[Message],
        schema: type[BaseModel],
        *,
        effort: str | None = None,
        max_tokens: int = 4096,
        private: bool = True,
    ) -> ClientReply:
        chat: list[dict[str, str]] = [{"role": m.role, "content": m.content} for m in messages]
        if model.supports_json_schema:
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": strict_schema(schema),
                                "strict": True},
            }  # fmt: skip
        else:
            response_format = {"type": "json_object"}
            chat = _with_instruction(chat, schema_instruction(schema))
        extra_body: dict[str, Any] = {}
        if self.provider.name == "openrouter" and private and self._deny:
            extra_body["provider"] = {"data_collection": "deny"}
        try:
            request: dict[str, Any] = {
                "model": model.model,
                "messages": chat,
                "response_format": response_format,
                "temperature": 0,
                "max_tokens": max_tokens,
                "extra_body": extra_body or None,
            }
            raw = await self._client.chat.completions.with_raw_response.create(**request)
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except openai.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc), _retry_after(exc.response.headers)) from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMServerError(f"{exc.status_code}: {exc.message}") from exc
            raise LLMError(f"{exc.status_code}: {exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise LLMServerError(f"connection: {exc}") from exc
        completion = raw.parse()
        headers = {k.lower(): v for k, v in raw.headers.items()}
        if not completion.choices:
            raise LLMServerError("no choices in the reply")
        choice = completion.choices[0]
        if getattr(choice.message, "refusal", None) or choice.finish_reason == "content_filter":
            raise LLMRefusalError(str(getattr(choice.message, "refusal", "") or "content_filter"))
        text = choice.message.content or ""
        parsed = parse_json(text, schema)
        return ClientReply(parsed=parsed, usage=_usage(completion.usage), raw_text=text,
                           headers=headers)  # fmt: skip


def _with_instruction(chat: list[dict[str, str]], instruction: str) -> list[dict[str, str]]:
    if chat and chat[0]["role"] == "system":
        return [{"role": "system", "content": f"{chat[0]['content']}\n\n{instruction}"}, *chat[1:]]
    return [{"role": "system", "content": instruction}, *chat]


def _usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    cost = getattr(usage, "cost", None)
    extra = getattr(usage, "model_extra", None) or {}
    cost = cost if cost is not None else extra.get("cost")
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details is not None else 0
    return Usage(
        input_tokens=int(usage.prompt_tokens or 0) - int(cached),
        output_tokens=int(usage.completion_tokens or 0),
        cache_read_tokens=int(cached),
        reported_cost_usd=Decimal(str(cost)) if cost is not None else None,
    )


def _retry_after(headers: Any) -> float | None:
    value = headers.get("retry-after") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
