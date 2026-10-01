"""
Claude through the native Anthropic SDK (plan M6; written against the claude-api skill and the
installed ``anthropic`` 1.x bindings).

* ``AsyncAnthropic(timeout=..., max_retries=0)`` - the router owns retries and fallbacks.
* Structured output: ``client.beta.messages.parse(output_format=<Pydantic model>)`` and
  ``response.parsed_output``. The beta endpoint is used because the server-side refusal
  ``fallbacks`` parameter lives there; the installed SDK accepts both on the same call.
* Effort: ``output_config={"effort": ...}`` per role (``low`` for veto/classification). Claude
  Opus 5.5's thinking cannot be disabled and its default effort is ``medium``, so the role sets
  it explicitly. Not sent to Haiku 4.5, which rejects it.
* Refusals: ``stop_reason == "refusal"`` → :class:`LLMRefusalError` (the caller's ABSTAIN). With
  ``LLM_ANTHROPIC_FALLBACKS`` (default on) the request opts into ``fallbacks: "default"`` (beta
  ``server-side-fallback-2026-07-01``) on the models that accept it, so a policy decline is
  re-run server-side on Anthropic's recommended model; a final refusal means the chain refused.
* Usage: ``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
  ``cache_creation_input_tokens``. Prompts are below the cacheable minimum, so no prompt
  caching; the router keeps its own response cache.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from src.llm.clients.base import parse_json
from src.llm.registry import ModelSpec
from src.llm.types import (
    ClientReply,
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMServerError,
    LLMTimeoutError,
    Message,
    Usage,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side `fallbacks: "default"` form (claude-api skill, 2026-09).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1")
NO_EFFORT_PREFIXES = ("claude-haiku-", "claude-sonnet-4-5", "claude-3")


class AnthropicNativeClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        timeout_s: float,
        fallbacks: bool = True,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._fallbacks = fallbacks
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=0
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
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        convo: list[Any] = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system"] = system
        if effort and not model.model.startswith(NO_EFFORT_PREFIXES):
            kwargs["output_config"] = {"effort": effort}
        if self._fallbacks and model.model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            response = await self._client.beta.messages.parse(
                model=model.model,
                max_tokens=max_tokens,
                messages=convo,
                output_format=schema,
                **kwargs,
            )
        except anthropic.APITimeoutError as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc), _retry_after(exc.response.headers)) from exc
        except anthropic.APIStatusError as exc:  # 529 overloaded and 503 are >= 500 too
            if exc.status_code >= 500:
                raise LLMServerError(f"{exc.status_code}: {exc.message}") from exc
            raise LLMError(f"{exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMServerError(f"connection: {exc}") from exc
        except (ValidationError, ValueError) as exc:  # parse() could not validate the reply
            raise LLMInvalidOutputError(f"reply does not match {schema.__name__}: {exc}") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMRefusalError(f"refused (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMInvalidOutputError("reply truncated at max_tokens")
        text = "".join(b.text for b in response.content if b.type == "text")
        parsed = response.parsed_output
        if not isinstance(parsed, schema):
            parsed = parse_json(text, schema)
        iterations = getattr(response.usage, "iterations", None) or []
        return ClientReply(
            parsed=parsed,
            usage=Usage(
                input_tokens=response.usage.input_tokens or 0,
                output_tokens=response.usage.output_tokens or 0,
                cache_read_tokens=response.usage.cache_read_input_tokens or 0,
                cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
            ),
            raw_text=text,
            extra={
                "served_model": response.model,
                "fallback": any(getattr(i, "type", "") == "fallback_message" for i in iterations),
            },
        )


def _retry_after(headers: Any) -> float | None:
    value = headers.get("retry-after") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
