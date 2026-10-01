"""
The LLM router (plan M6): ``LLMRouter.complete(role, messages, schema, ...)`` - the only way the
system calls an LLM.

For a role it tries ``[primary, *fallbacks]`` in order and returns an :class:`LLMResult`; it
**never raises** for an LLM failure (the caller maps a failed result to ABSTAIN - an LLM can
never block or approve anything by failing). Per attempt it:

1. skips a model whose **circuit breaker** is open (settings-driven, per provider:model - the
   legacy breaker was hardcoded, audit F-18) or whose provider is **paused** by a 429's
   ``retry-after`` or exhausted ``x-ratelimit-*`` headers;
2. applies the **budget gate** - the persisted daily INR caps (total and per role) and the
   per-decision cap; a paid call over a cap is ``BUDGET_EXCEEDED`` (free models still run);
3. serves an identical earlier reply from the **response cache** (key = sha256 of the prompt
   version, model and rendered prompt), stored in the event store's ``kv_state``;
4. calls the client under ``asyncio.wait_for`` (the timeout);
5. falls back on timeout, 429, 5xx, invalid output, refusal or any other client error;
6. records an ``LLMCall`` event (role, provider, model, prompt version/sha, tokens, latency,
   cost in USD and INR, cache hit, outcome, decision id).

Spend is accounted from those events: :meth:`SpendLedger.from_events` rebuilds today's ledger at
startup, so a restart never resets a budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from src.domain.clock import Clock, now_ist
from src.domain.events import Event, LLMCall, LLMOutcome
from src.domain.sink import EventSink
from src.llm.clients.base import LLMClient
from src.llm.pricing import PricingTable, to_inr
from src.llm.registry import ModelSpec, RoleConfig
from src.llm.types import (
    BudgetExceededError,
    LLMError,
    LLMRateLimitedError,
    Message,
    Usage,
)
from src.store.event_store import EventStore

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
ZERO = Decimal(0)

# Output room per role (thinking models need headroom beyond the JSON itself).
MAX_TOKENS = {"veto": 2048, "explain": 2048, "label": 4096, "review": 8192, "research": 16000}
# Roles whose prompts carry non-public context (positions, decisions, P&L).
PRIVATE_ROLES = frozenset({"veto", "review", "explain", "research"})


class ResponseCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, value: str, ts: datetime) -> None: ...


class MemoryResponseCache:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.rows.get(key)

    def put(self, key: str, value: str, ts: datetime) -> None:
        self.rows[key] = value


class StoreResponseCache:
    """Replies kept in the event store's ``kv_state`` (namespace ``llm_cache``)."""

    def __init__(self, store: EventStore, namespace: str = "llm_cache") -> None:
        self._store, self._namespace = store, namespace

    def get(self, key: str) -> str | None:
        return self._store.kv_get(self._namespace, key)

    def put(self, key: str, value: str, ts: datetime) -> None:
        self._store.kv_put(self._namespace, key, value, ts)


class ClientSource(Protocol):
    """Anything that returns the client for a provider (``ClientFactory`` in production)."""

    def get(self, provider: str) -> LLMClient: ...


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    role: str
    outcome: LLMOutcome | str  # an LLMOutcome, or "disabled" when the role is off
    parsed: T | None = None
    model: str | None = None  # the provider:model that answered
    attempts: int = 0
    cost_inr: Decimal = ZERO
    cache_hit: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.parsed is not None


@dataclass(frozen=True)
class BudgetLimits:
    daily_inr: Decimal = ZERO  # 0 = unlimited
    per_role_daily_inr: Mapping[str, Decimal] = field(default_factory=dict)
    per_decision_inr: Decimal = ZERO

    @classmethod
    def from_settings(cls, settings: Any) -> BudgetLimits:
        return cls(
            daily_inr=Decimal(str(settings.llm_budget_daily_inr)),
            per_role_daily_inr={k: Decimal(str(v))
                                for k, v in dict(settings.llm_budget_role_daily_inr).items()},
            per_decision_inr=Decimal(str(settings.llm_budget_per_decision_inr)),
        )  # fmt: skip


@dataclass
class SpendLedger:
    """INR spent today (IST), in total, per role and per decision."""

    day: date
    total: Decimal = ZERO
    roles: dict[str, Decimal] = field(default_factory=dict)
    decisions: dict[str, Decimal] = field(default_factory=dict)

    @classmethod
    def from_events(cls, events: Iterable[Event], day: date) -> SpendLedger:
        ledger = cls(day)
        for event in events:
            p = event.payload
            if isinstance(p, LLMCall) and event.ist_date == day and p.cost_inr:
                ledger.add(p.role, p.decision_id, p.cost_inr)
        return ledger

    def add(self, role: str, decision_id: str | None, inr: Decimal) -> None:
        self.total += inr
        self.roles[role] = self.roles.get(role, ZERO) + inr
        if decision_id:
            self.decisions[decision_id] = self.decisions.get(decision_id, ZERO) + inr

    def exceeded(self, role: str, decision_id: str | None, limits: BudgetLimits) -> str | None:
        if limits.daily_inr and self.total >= limits.daily_inr:
            return f"daily LLM budget Rs {limits.daily_inr} spent"
        role_cap = limits.per_role_daily_inr.get(role, ZERO)
        if role_cap and self.roles.get(role, ZERO) >= role_cap:
            return f"daily {role} budget Rs {role_cap} spent"
        if decision_id and limits.per_decision_inr:
            if self.decisions.get(decision_id, ZERO) >= limits.per_decision_inr:
                return f"per-decision budget Rs {limits.per_decision_inr} spent"
        return None


@dataclass
class _Breaker:
    failures: int = 0
    open_until: datetime | None = None


class LLMRouter:
    def __init__(
        self,
        *,
        roles: Mapping[str, RoleConfig],
        clients: ClientSource,
        pricing: PricingTable,
        sink: EventSink,
        clock: Clock,
        usd_inr: float,
        timeout_s: float,
        budgets: BudgetLimits | None = None,
        ledger: SpendLedger | None = None,
        cache: ResponseCache | None = None,
        breaker_failures: int = 3,
        breaker_cooldown_s: float = 120.0,
    ) -> None:
        self.roles = dict(roles)
        self._clients = clients
        self._pricing = pricing
        self._sink = sink
        self._clock = clock
        self._usd_inr = usd_inr
        self._timeout_s = timeout_s
        self._budgets = budgets or BudgetLimits()
        self.ledger = ledger or SpendLedger(now_ist(clock).date())
        self._cache = cache
        self._breaker_failures = breaker_failures
        self._breaker_cooldown = timedelta(seconds=breaker_cooldown_s)
        self._breakers: dict[str, _Breaker] = {}
        self._paused_until: dict[str, datetime] = {}  # provider -> rate-limit pause

    async def complete(
        self,
        role: str,
        messages: Sequence[Message],
        schema: type[T],
        *,
        prompt_version: str,
        decision_id: str | None = None,
        book_id: str | None = None,
    ) -> LLMResult[T]:
        config = self.roles.get(role)
        if config is None or not config.enabled:
            return LLMResult(role, "disabled", error=f"role {role} is not configured")
        rendered = json.dumps([[m.role, m.content] for m in messages], ensure_ascii=False)
        prompt_sha = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        self._roll_day()

        last = LLMOutcome.ERROR
        error: str | None = None
        for attempt, model in enumerate(config.chain):
            outcome, result, error = await self._attempt(
                role, config, model, attempt, messages, schema, rendered, prompt_version,
                prompt_sha, decision_id, book_id,
            )  # fmt: skip
            if result is not None:
                return LLMResult(role, LLMOutcome.OK, result[0], model.spec, attempt + 1,
                                 result[1], result[2])  # fmt: skip
            last = outcome
        return LLMResult(role, last, attempts=len(config.chain), error=error)

    # -- one model ------------------------------------------------------------------------------

    async def _attempt(
        self,
        role: str,
        config: RoleConfig,
        model: ModelSpec,
        attempt: int,
        messages: Sequence[Message],
        schema: type[T],
        rendered: str,
        prompt_version: str,
        prompt_sha: str,
        decision_id: str | None,
        book_id: str | None,
    ) -> tuple[LLMOutcome, tuple[T, Decimal, bool] | None, str | None]:
        def record(outcome: LLMOutcome, *, usage: Usage | None = None, latency_ms: float = 0.0,
                   usd: Decimal | None = None, inr: Decimal | None = None, hit: bool = False,
                   error: str | None = None) -> None:  # fmt: skip
            u = usage or Usage()
            self._sink.emit(
                LLMCall(decision_id=decision_id, book_id=book_id, role=role,
                        provider=model.provider, model=model.model, attempt=attempt,
                        prompt_version=prompt_version, prompt_sha=prompt_sha,
                        tokens_in=u.input_tokens, tokens_out=u.output_tokens,
                        cache_read_tokens=u.cache_read_tokens,
                        cache_write_tokens=u.cache_write_tokens, latency_ms=latency_ms,
                        cost_usd=usd, cost_inr=inr, cache_hit=hit, outcome=outcome, error=error),
                source="llm",
            )  # fmt: skip

        key = hashlib.sha256(f"{prompt_version}\n{model.spec}\n{rendered}".encode()).hexdigest()
        cached = self._cached(key, schema)
        if cached is not None:
            record(LLMOutcome.OK, usd=ZERO, inr=ZERO, hit=True)
            return LLMOutcome.OK, (cached, ZERO, True), None

        now = self._clock.now()
        breaker = self._breakers.setdefault(model.spec, _Breaker())
        if breaker.open_until is not None and now < breaker.open_until:
            reason = f"circuit open until {breaker.open_until:%H:%M:%S}"
            record(LLMOutcome.BREAKER_OPEN, error=reason)
            return LLMOutcome.BREAKER_OPEN, None, reason
        paused = self._paused_until.get(model.provider)
        if paused is not None and now < paused:
            reason = f"{model.provider} rate-limited until {paused:%H:%M:%S}"
            record(LLMOutcome.RATE_LIMITED, error=reason)
            return LLMOutcome.RATE_LIMITED, None, reason
        price = self._pricing.price(model)
        free = price is not None and price.input_per_m == 0 and price.output_per_m == 0
        over = None if free else self.ledger.exceeded(role, decision_id, self._budgets)
        if over is not None:
            record(LLMOutcome.BUDGET_EXCEEDED, error=over)
            return LLMOutcome.BUDGET_EXCEEDED, None, str(BudgetExceededError(over))

        started = time.perf_counter()
        try:
            reply = await asyncio.wait_for(
                self._clients.get(model.provider).complete(
                    model, messages, schema, effort=config.effort,
                    max_tokens=MAX_TOKENS.get(role, 4096), private=role in PRIVATE_ROLES,
                ),
                self._timeout_s,
            )  # fmt: skip
        except TimeoutError:
            self._failed(breaker, now)
            record(LLMOutcome.TIMEOUT, latency_ms=_ms(started), error="timeout")
            return LLMOutcome.TIMEOUT, None, f"{model.spec}: timeout"
        except LLMError as exc:
            outcome = LLMOutcome(exc.outcome)
            if isinstance(exc, LLMRateLimitedError):
                self._pause(model.provider, now, exc.retry_after_s or 60.0)
            elif outcome in (LLMOutcome.SERVER_ERROR, LLMOutcome.ERROR, LLMOutcome.TIMEOUT):
                self._failed(breaker, now)
            record(outcome, latency_ms=_ms(started), error=str(exc)[:500])
            return outcome, None, f"{model.spec}: {exc}"
        except Exception as exc:  # a client bug never escapes the router
            logger.exception("LLM client %s failed", model.spec)
            self._failed(breaker, now)
            record(LLMOutcome.ERROR, latency_ms=_ms(started), error=f"{type(exc).__name__}: {exc}")
            return LLMOutcome.ERROR, None, f"{model.spec}: {type(exc).__name__}"

        breaker.failures, breaker.open_until = 0, None
        self._read_rate_headers(model.provider, now, reply.headers)
        usd = self._pricing.cost_usd(model, reply.usage)
        inr = to_inr(usd, self._usd_inr)
        if inr:
            self.ledger.add(role, decision_id, inr)
        record(LLMOutcome.OK, usage=reply.usage, latency_ms=_ms(started), usd=usd, inr=inr)
        if usd is None:
            logger.warning("no price for %s: its spend is recorded as unknown", model.spec)
        parsed = reply.parsed
        if not isinstance(parsed, schema):  # pragma: no cover - clients validate already
            return LLMOutcome.INVALID_OUTPUT, None, "client returned the wrong schema"
        self._store(key, parsed)
        return LLMOutcome.OK, (parsed, inr or ZERO, False), None

    # -- state ----------------------------------------------------------------------------------

    def _roll_day(self) -> None:
        today = now_ist(self._clock).date()
        if self.ledger.day != today:
            self.ledger = SpendLedger(today)

    def _failed(self, breaker: _Breaker, now: datetime) -> None:
        breaker.failures += 1
        if breaker.failures >= self._breaker_failures:
            breaker.open_until = now + self._breaker_cooldown
            breaker.failures = 0

    def _pause(self, provider: str, now: datetime, seconds: float) -> None:
        until = now + timedelta(seconds=max(1.0, min(seconds, 3600.0)))
        self._paused_until[provider] = max(until, self._paused_until.get(provider, until))

    def _read_rate_headers(self, provider: str, now: datetime, headers: Mapping[str, str]) -> None:
        """Pause a provider whose rate-limit headers say the window is used up."""
        for remaining, reset in (
            ("x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
            ("x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
        ):
            if headers.get(remaining) == "0":
                seconds = _duration_s(headers.get(reset, ""))
                if seconds is not None:
                    self._pause(provider, now, seconds)

    def _cached(self, key: str, schema: type[T]) -> T | None:
        row = self._cache.get(key) if self._cache is not None else None
        if row is None:
            return None
        try:
            return schema.model_validate_json(row)
        except ValidationError:
            return None

    def _store(self, key: str, parsed: BaseModel) -> None:
        if self._cache is not None:
            self._cache.put(key, parsed.model_dump_json(), self._clock.now())


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _duration_s(text: str) -> float | None:
    """Groq/OpenAI reset durations: ``"1s"``, ``"6ms"``, ``"2m59.56s"``, ``"1h2m"``."""
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total, number = 0.0, ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isdigit() or ch == ".":
            number += ch
            i += 1
            continue
        unit = "ms" if text.startswith("ms", i) else ch
        i += len(unit)
        if not number:
            return None
        value = float(number)
        number = ""
        total += {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}.get(unit, float("nan")) * value
    if number:
        total += float(number)
    return total if total == total else None  # NaN -> unknown unit
