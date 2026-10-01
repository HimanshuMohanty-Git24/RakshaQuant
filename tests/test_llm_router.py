"""Plan M6: the LLM router - fallback chain, budgets, breakers, rate limits, cache, LLMCall."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from src.domain.clock import ReplayClock
from src.domain.events import LLMOutcome
from src.domain.sink import RecordingSink
from src.llm.pricing import PricingTable
from src.llm.registry import ModelSpec, RoleConfig
from src.llm.router import (
    BudgetLimits,
    LLMRouter,
    MemoryResponseCache,
    SpendLedger,
    StoreResponseCache,
    _duration_s,
)
from src.llm.types import (
    ClientReply,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMServerError,
    Message,
    Usage,
)
from src.store.event_store import EventStore
from src.store.sink import StoreSink

T0 = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
MSGS = [Message("system", "s"), Message("user", "u")]
SONNET, GROQ, FREE = (
    "anthropic:claude-sonnet-5-5",
    "groq:llama-3.3-70b-versatile",
    "openrouter:x/y:free",
)


class Out(BaseModel):
    verdict: Literal["APPROVE", "VETO", "ABSTAIN"]


class Fake:
    """A scripted client: each call pops the next behaviour (an exception, 'slow', or a reply)."""

    def __init__(
        self, *script: Any, usage: Usage | None = None, headers: dict[str, str] | None = None
    ):
        self.script = list(script)
        self.calls: list[str] = []
        self.usage = usage or Usage(input_tokens=1000, output_tokens=200)
        self.headers = headers or {}

    async def complete(self, model: ModelSpec, messages: Sequence[Message], schema: type[BaseModel],
                       **kw: Any) -> ClientReply:  # fmt: skip
        self.calls.append(model.spec)
        step = self.script.pop(0) if self.script else "ok"
        if step == "slow":
            await asyncio.sleep(5)
        if isinstance(step, Exception):
            raise step
        return ClientReply(parsed=schema.model_validate({"verdict": "VETO"}), usage=self.usage,
                           headers=self.headers)  # fmt: skip


class Clients:
    def __init__(self, **by_provider: Fake) -> None:
        self.by = by_provider

    def get(self, provider: str) -> Fake:
        return self.by[provider]


def router(clients: Clients, *chain: str, clock: ReplayClock | None = None, sink=None, **kw: Any):
    clock = clock or ReplayClock(T0)
    sink = sink or RecordingSink(clock)
    roles = {"veto": RoleConfig("veto", tuple(ModelSpec.parse(c) for c in chain), "low")}
    args: dict[str, Any] = {"roles": roles, "clients": clients, "pricing": PricingTable.from_yaml(),
                            "sink": sink, "clock": clock, "usd_inr": 88.0, "timeout_s": 0.2}  # fmt: skip
    args.update(kw)
    return LLMRouter(**args), sink, clock


def calls(sink: RecordingSink) -> list[tuple[str, str]]:
    return [(e.payload.model, e.payload.outcome.value) for e in sink.events if e.type == "LLMCall"]


async def test_a_good_primary_answers_and_is_accounted():
    anthropic = Fake()
    r, sink, _ = router(Clients(anthropic=anthropic), SONNET)
    result = await r.complete("veto", MSGS, Out, prompt_version="veto_v1", decision_id="d1")
    assert result.ok and result.parsed == Out(verdict="VETO") and result.model == SONNET
    (call,) = [e.payload for e in sink.events]
    assert (call.tokens_in, call.tokens_out, call.role, call.attempt) == (1000, 200, "veto", 0)
    assert call.cost_usd == Decimal("0.004") and call.cost_inr == Decimal("0.3520")
    assert (
        call.decision_id == "d1" and call.prompt_version == "veto_v1" and len(call.prompt_sha) == 64
    )
    assert r.ledger.total == Decimal("0.3520") and r.ledger.decisions["d1"] == Decimal("0.3520")


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [("slow", "timeout"), (LLMRateLimitedError("429", 30), "rate_limited"),
     (LLMInvalidOutputError("bad json"), "invalid_output"), (LLMRefusalError("no"), "refusal"),
     (LLMServerError("502"), "server_error")],
)  # fmt: skip
async def test_the_fallback_chain(failure, outcome):
    r, sink, _ = router(Clients(openrouter=Fake(failure), groq=Fake()), FREE, GROQ)
    result = await r.complete("veto", MSGS, Out, prompt_version="v1")
    assert result.ok and result.model == GROQ and result.attempts == 2
    assert calls(sink) == [("x/y:free", outcome), ("llama-3.3-70b-versatile", "ok")]


async def test_when_every_model_fails_the_router_returns_never_raises():
    boom = Fake(RuntimeError("client bug"))
    r, sink, _ = router(Clients(openrouter=Fake(LLMRefusalError("no")), groq=boom), FREE, GROQ)
    result = await r.complete("veto", MSGS, Out, prompt_version="v1")
    assert not result.ok and result.parsed is None and result.outcome == LLMOutcome.ERROR
    assert [o for _, o in calls(sink)] == ["refusal", "error"]


async def test_a_disabled_role_makes_no_call():
    r, sink, _ = router(Clients(), SONNET)
    result = await r.complete("review", MSGS, Out, prompt_version="v1")
    assert result.outcome == "disabled" and not result.ok and sink.events == []


async def test_the_budget_gate_gives_budget_exceeded_but_free_models_still_run():
    paid = Fake()
    limits = BudgetLimits(daily_inr=Decimal("0.5"))
    r, sink, _ = router(Clients(anthropic=paid, openrouter=Fake()), SONNET, FREE, budgets=limits)
    assert (await r.complete("veto", MSGS, Out, prompt_version="a")).model == SONNET  # Rs 0.352
    assert (await r.complete("veto", MSGS, Out, prompt_version="b")).model == SONNET  # Rs 0.704
    third = await r.complete("veto", MSGS, Out, prompt_version="c")
    assert third.model == FREE and len(paid.calls) == 2  # the paid model was not called
    assert calls(sink)[-2:] == [("claude-sonnet-5-5", "budget_exceeded"), ("x/y:free", "ok")]
    only_paid, _, _ = router(Clients(anthropic=Fake()), SONNET, budgets=limits, ledger=r.ledger)
    blocked = await only_paid.complete("veto", MSGS, Out, prompt_version="d")
    assert blocked.outcome == LLMOutcome.BUDGET_EXCEEDED and not blocked.ok  # -> ABSTAIN


async def test_role_and_per_decision_caps():
    limits = BudgetLimits(per_role_daily_inr={"veto": Decimal("0.3")},
                          per_decision_inr=Decimal("1"))  # fmt: skip
    r, _, _ = router(Clients(anthropic=Fake()), SONNET, budgets=limits)
    assert (await r.complete("veto", MSGS, Out, prompt_version="a")).ok
    assert (await r.complete("veto", MSGS, Out, prompt_version="b")).outcome == "budget_exceeded"
    per_decision = BudgetLimits(per_decision_inr=Decimal("0.3"))
    r2, _, _ = router(Clients(anthropic=Fake()), SONNET, budgets=per_decision)
    assert (await r2.complete("veto", MSGS, Out, prompt_version="a", decision_id="d1")).ok
    second = await r2.complete("veto", MSGS, Out, prompt_version="b", decision_id="d1")
    assert second.outcome == "budget_exceeded"
    assert (await r2.complete("veto", MSGS, Out, prompt_version="c", decision_id="d2")).ok


async def test_spend_survives_a_restart_through_the_events(tmp_path):
    clock = ReplayClock(T0)
    with EventStore(tmp_path / "rq.db") as store:
        sink = StoreSink(store, clock, "llm")
        r, _, _ = router(Clients(anthropic=Fake()), SONNET, clock=clock, sink=sink)
        await r.complete("veto", MSGS, Out, prompt_version="a", decision_id="d1")
        ledger = SpendLedger.from_events(store.read(types=["LLMCall"]), date_ist(clock))
        assert ledger.total == Decimal("0.3520") and ledger.roles == {"veto": Decimal("0.3520")}


def date_ist(clock: ReplayClock):
    from src.domain.clock import now_ist

    return now_ist(clock).date()


async def test_the_breaker_opens_after_consecutive_failures_and_recovers():
    down = Fake(*[LLMServerError("500")] * 3)
    r, sink, clock = router(Clients(groq=down), GROQ, breaker_failures=3, breaker_cooldown_s=120)
    for n in range(3):
        assert not (await r.complete("veto", MSGS, Out, prompt_version=f"p{n}")).ok
    skipped = await r.complete("veto", MSGS, Out, prompt_version="p3")
    assert skipped.outcome == LLMOutcome.BREAKER_OPEN and len(down.calls) == 3
    await clock.advance(121)
    assert (await r.complete("veto", MSGS, Out, prompt_version="p4")).ok  # half-open trial


async def test_a_429_pauses_the_provider_and_headers_can_too():
    limited = Fake(LLMRateLimitedError("429", retry_after_s=30))
    r, sink, clock = router(Clients(groq=limited), GROQ)
    await r.complete("veto", MSGS, Out, prompt_version="a")
    paused = await r.complete("veto", MSGS, Out, prompt_version="b")
    assert paused.outcome == LLMOutcome.RATE_LIMITED and len(limited.calls) == 1
    await clock.advance(31)
    assert (await r.complete("veto", MSGS, Out, prompt_version="c")).ok

    exhausted = Fake(headers={"x-ratelimit-remaining-requests": "0",
                              "x-ratelimit-reset-requests": "2m0.5s"})  # fmt: skip
    r2, _, clock2 = router(Clients(groq=exhausted), GROQ)
    assert (await r2.complete("veto", MSGS, Out, prompt_version="a")).ok
    assert (await r2.complete("veto", MSGS, Out, prompt_version="b")).outcome == "rate_limited"
    await clock2.advance(121)
    assert (await r2.complete("veto", MSGS, Out, prompt_version="c")).ok


async def test_identical_prompts_are_served_from_the_cache(tmp_path):
    clock = ReplayClock(T0)
    with EventStore(tmp_path / "rq.db") as store:
        client = Fake()
        r, sink, _ = router(Clients(anthropic=client), SONNET, clock=clock,
                            cache=StoreResponseCache(store))  # fmt: skip
        first = await r.complete("veto", MSGS, Out, prompt_version="veto_v1")
        again = await r.complete("veto", MSGS, Out, prompt_version="veto_v1")
        assert first.ok and again.cache_hit and again.cost_inr == 0 and len(client.calls) == 1
        assert sink.events[-1].payload.cache_hit
        changed = await r.complete("veto", MSGS, Out, prompt_version="veto_v2")
        assert not changed.cache_hit and len(client.calls) == 2
        # A new process reuses the stored reply.
        reborn, _, _ = router(Clients(anthropic=Fake()), SONNET, clock=clock,
                              cache=StoreResponseCache(store))  # fmt: skip
        assert (await reborn.complete("veto", MSGS, Out, prompt_version="veto_v1")).cache_hit


async def test_the_budget_day_rolls_over_in_ist():
    clock = ReplayClock(datetime(2026, 10, 5, 18, 0, tzinfo=UTC))  # 23:30 IST
    r, _, _ = router(Clients(anthropic=Fake()), SONNET, clock=clock,
                     budgets=BudgetLimits(daily_inr=Decimal("0.3")), cache=MemoryResponseCache())  # fmt: skip
    assert (await r.complete("veto", MSGS, Out, prompt_version="a")).ok
    assert not (await r.complete("veto", MSGS, Out, prompt_version="b")).ok
    await clock.advance(3600)  # 00:30 IST: a new budget day
    assert (await r.complete("veto", MSGS, Out, prompt_version="c")).ok


def test_rate_limit_reset_durations():
    assert _duration_s("1s") == 1.0 and _duration_s("6ms") == pytest.approx(0.006)
    assert _duration_s("2m59.56s") == pytest.approx(179.56) and _duration_s("1h2m") == 3720.0
    assert _duration_s("30") == 30.0 and _duration_s("") is None and _duration_s("5x") is None
