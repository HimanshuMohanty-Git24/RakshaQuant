"""Plan M6: the provider clients against recorded wire fixtures (httpx2 mock transports) - the
exact request each SDK sends, and how replies, refusals and errors are normalised."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Literal

import anthropic
import httpx2
import openai
import pytest
from pydantic import BaseModel

from src.llm.clients.anthropic_native import FALLBACK_BETA, AnthropicNativeClient
from src.llm.clients.base import strict_schema
from src.llm.clients.openai_compat import OpenAICompatClient
from src.llm.registry import PROVIDERS, ModelSpec
from src.llm.types import (
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMServerError,
    Message,
)


class Verdict(BaseModel):
    verdict: Literal["APPROVE", "VETO", "ABSTAIN"]
    confidence: float
    title: str = ""  # a property literally named "title" must survive the strict schema


MESSAGES = [Message("system", "You review trades."), Message("user", "<data>{}</data>")]
GOOD = {"verdict": "VETO", "confidence": 0.7, "title": "results tomorrow"}


class Wire:
    """A recorded exchange: captures each request and answers with a canned reply."""

    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body, headers=self.headers)

    @property
    def sent(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def claude(wire: Wire, *, fallbacks: bool = True) -> AnthropicNativeClient:
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wire))
    sdk = anthropic.AsyncAnthropic(api_key="test-key", max_retries=0, http_client=http)
    return AnthropicNativeClient(api_key="test-key", timeout_s=5, fallbacks=fallbacks, client=sdk)


def message(text: str, *, stop: str = "end_turn", model: str = "claude-opus-5-5",
            stop_details: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "id": "msg_01", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop, "stop_sequence": None, "stop_details": stop_details,
        "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    }  # fmt: skip


# --- Anthropic ------------------------------------------------------------------------------------


async def test_claude_request_shape_effort_fallbacks_and_parsed_output():
    wire = Wire(body=message(json.dumps(GOOD)))
    reply = await claude(wire).complete(ModelSpec.parse("anthropic:claude-opus-5-5"), MESSAGES,
                                        Verdict, effort="low", max_tokens=2048)  # fmt: skip
    sent = wire.sent
    assert sent["model"] == "claude-opus-5-5" and sent["max_tokens"] == 2048
    assert sent["system"] == "You review trades."
    assert sent["messages"] == [{"role": "user", "content": "<data>{}</data>"}]
    assert sent["output_config"]["effort"] == "low"  # merged with the parse() format
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["fallbacks"] == "default"
    assert FALLBACK_BETA in wire.requests[-1].headers["anthropic-beta"]
    assert reply.parsed == Verdict(**GOOD)
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (120, 30)
    assert reply.extra == {"served_model": "claude-opus-5-5", "fallback": False}


async def test_haiku_gets_neither_effort_nor_fallbacks():
    wire = Wire(body=message(json.dumps(GOOD), model="claude-haiku-4-5"))
    await claude(wire).complete(ModelSpec.parse("anthropic:claude-haiku-4-5"), MESSAGES, Verdict,
                                effort="low")  # fmt: skip
    assert "effort" not in wire.sent.get("output_config", {}) and "fallbacks" not in wire.sent
    assert FALLBACK_BETA not in wire.requests[-1].headers.get("anthropic-beta", "")


async def test_claude_fallbacks_can_be_switched_off():
    wire = Wire(body=message(json.dumps(GOOD)))
    await claude(wire, fallbacks=False).complete(ModelSpec.parse("anthropic:claude-sonnet-5-5"),
                                                 MESSAGES, Verdict)  # fmt: skip
    assert "fallbacks" not in wire.sent


@pytest.mark.parametrize(
    ("wire", "error"),
    [
        (Wire(body=message("", stop="refusal", stop_details={
            "type": "refusal", "category": "cyber", "explanation": "declined"})), LLMRefusalError),
        (Wire(body=message('{"verdict": "VETO"', stop="max_tokens")), LLMInvalidOutputError),
        (Wire(body=message('{"verdict": "MAYBE", "confidence": 2}')), LLMInvalidOutputError),
        (Wire(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
              {"retry-after": "7"}), LLMRateLimitedError),
        (Wire(529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
         LLMServerError),
        (Wire(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}),
         LLMError),
    ],
)  # fmt: skip
async def test_claude_outcomes_are_normalised(wire, error):
    with pytest.raises(error) as caught:
        await claude(wire).complete(ModelSpec.parse("anthropic:claude-opus-5-5"), MESSAGES, Verdict)
    if isinstance(caught.value, LLMRateLimitedError):
        assert caught.value.retry_after_s == 7.0


# --- OpenAI-compatible ---------------------------------------------------------------------------


def completion(content: str | None, *, usage: dict[str, Any] | None = None,
               finish: str = "stop", refusal: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "id": "cmpl-1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": content, "refusal": refusal}}],
        "usage": usage or {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240},
    }  # fmt: skip


def compat(provider: str, wire: Wire, **kw: Any) -> OpenAICompatClient:
    spec = PROVIDERS[provider]
    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wire))
    headers = dict(spec.default_headers)
    if kw.get("referer"):
        headers["HTTP-Referer"] = kw["referer"]
    sdk = openai.AsyncOpenAI(api_key="test-key", base_url=spec.base_url or "https://api.test/v1",
                             max_retries=0, http_client=http, default_headers=headers or None)  # fmt: skip
    return OpenAICompatClient(
        spec, api_key="test-key", base_url=None, timeout_s=5, client=sdk, **kw
    )


async def test_openrouter_free_model_json_mode_privacy_and_reported_cost():
    usage = {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240, "cost": 0.00042}
    wire = Wire(
        body=completion(json.dumps(GOOD), usage=usage),
        headers={"x-ratelimit-remaining-requests": "19"},
    )
    client = compat("openrouter", wire, referer="https://example.invalid/rq")
    reply = await client.complete(ModelSpec.parse("openrouter:meta-llama/llama-3.3-70b-instruct:free"),
                                  MESSAGES, Verdict)  # fmt: skip
    sent, headers = wire.sent, wire.requests[-1].headers
    assert sent["model"] == "meta-llama/llama-3.3-70b-instruct:free" and sent["temperature"] == 0
    assert sent["response_format"] == {"type": "json_object"}
    assert "JSON schema" in sent["messages"][0]["content"]  # the schema is in the system prompt
    assert sent["provider"] == {"data_collection": "deny"} and "usage" not in sent
    assert (
        headers["x-openrouter-title"] == "RakshaQuant"
        and headers["http-referer"] == "https://example.invalid/rq"
    )
    assert reply.parsed == Verdict(**GOOD) and reply.usage.reported_cost_usd == Decimal("0.00042")
    assert reply.headers["x-ratelimit-remaining-requests"] == "19"


async def test_openai_uses_strict_json_schema_and_public_prompts_are_not_marked():
    wire = Wire(body=completion(json.dumps(GOOD)))
    await compat("openai", wire).complete(ModelSpec.parse("openai:gpt-5-mini"), MESSAGES, Verdict)
    fmt = wire.sent["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == strict_schema(Verdict)
    assert "provider" not in wire.sent  # only OpenRouter routes to third parties
    groq = Wire(body=completion(json.dumps(GOOD)))
    reply = await compat("groq", groq).complete(ModelSpec.parse("groq:llama-3.3-70b-versatile"),
                                                MESSAGES, Verdict)  # fmt: skip
    assert groq.sent["response_format"] == {"type": "json_object"}
    assert reply.usage.input_tokens == 200 and reply.usage.reported_cost_usd is None


def test_the_strict_schema_closes_objects_and_keeps_a_title_property():
    schema = strict_schema(Verdict)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["verdict", "confidence", "title"]
    assert "title" in schema["properties"] and "title" not in schema  # keyword dropped, field kept
    assert "default" not in schema["properties"]["title"]


@pytest.mark.parametrize(
    ("wire", "error"),
    [
        (Wire(body=completion(None, refusal="I can't help with that.")), LLMRefusalError),
        (Wire(body=completion("", finish="content_filter")), LLMRefusalError),
        (Wire(body=completion("not json")), LLMInvalidOutputError),
        (Wire(body=completion("```json\n" + json.dumps({"verdict": "NO"}) + "\n```")),
         LLMInvalidOutputError),
        (Wire(429, {"error": {"message": "rate limited"}}, {"retry-after": "3"}), LLMRateLimitedError),
        (Wire(503, {"error": {"message": "down"}}), LLMServerError),
        (Wire(401, {"error": {"message": "bad key"}}), LLMError),
    ],
)  # fmt: skip
async def test_compat_outcomes_are_normalised(wire, error):
    with pytest.raises(error):
        await compat("groq", wire).complete(ModelSpec.parse("groq:llama-3.3-70b-versatile"),
                                            MESSAGES, Verdict)  # fmt: skip


async def test_a_fenced_json_reply_is_accepted():
    wire = Wire(body=completion("```json\n" + json.dumps(GOOD) + "\n```"))
    reply = await compat("ollama", wire).complete(
        ModelSpec.parse("ollama:llama3.2"), MESSAGES, Verdict
    )
    assert reply.parsed == Verdict(**GOOD)
