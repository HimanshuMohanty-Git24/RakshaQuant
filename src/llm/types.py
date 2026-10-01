"""Shared LLM types (plan M6): usage, a client's reply, and the failures the router acts on."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reported_cost_usd: Decimal | None = None  # e.g. OpenRouter's usage.cost


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ClientReply:
    """A schema-validated reply from one provider call."""

    parsed: BaseModel
    usage: Usage
    raw_text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """Base of the failures the router falls back on."""

    outcome = "error"


class LLMTimeoutError(LLMError):
    outcome = "timeout"


class LLMRateLimitedError(LLMError):
    outcome = "rate_limited"

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LLMServerError(LLMError):
    outcome = "server_error"


class LLMInvalidOutputError(LLMError):
    """The reply did not parse into the schema."""

    outcome = "invalid_output"


class LLMRefusalError(LLMError):
    """The model refused; the caller's answer becomes ABSTAIN."""

    outcome = "refusal"


class BudgetExceededError(LLMError):
    outcome = "budget_exceeded"
