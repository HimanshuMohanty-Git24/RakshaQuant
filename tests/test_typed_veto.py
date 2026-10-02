"""Plan M7.9: Book B's typed veto - verbalised features, public-only state, calibrated threshold."""

from __future__ import annotations

import typing
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import pytest

from src.decision.advisors.typed_veto import QUESTIONS, TypedVetoAdvisor, verbalise, veto_state
from src.decision_models.base import Answer, Question
from src.decision_models.cascade import Cascade, CascadeConfig
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.sink import RecordingSink
from src.domain.types import (
    AnnouncementType,
    EventDirection,
    Instrument,
    Materiality,
    Regime,
    TypedEvent,
    Verdict,
)
from src.features.technical import Features
from src.strategies.policy import Proposal, TradePolicy
from src.utils.market_time import IST
from tests.test_llm_prompts import one_proposal

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # Mon 09:30 IST
INFY = Instrument.nse_equity("INFY", name="Infosys Ltd.", sector="Information Technology")


def feats(**kw: Any) -> Features:
    base: dict[str, Any] = {
        "instrument_key": INFY.key, "bar_date": date(2026, 10, 1), "bars": 250, "open": 1000.0,
        "high": 1010.0, "low": 990.0, "close": 1000.0, "volume": 2e6, "prev_close": 990.0,
        "rsi_14": 78.0, "atr_14": 35.0, "adx_14": 32.0, "bb_percent": 1.2,
        "sma": {50: 950.0, 200: 1020.0}, "adv20_inr": 6e9,
    }  # fmt: skip
    base.update(kw)
    return Features(**base)


def test_verbalisation_is_deterministic_and_categorical():
    words = verbalise(feats())
    assert words == {
        "rsi": "RSI overbought", "volatility": "volatility high", "trend_strength": "strong trend",
        "vs_50d": "price above its 50-day average", "vs_200d": "price below its 200-day average",
        "bands": "price above the upper Bollinger band", "last_session": "ordinary last session",
        "liquidity": "liquidity high",
    }  # fmt: skip
    sparse = verbalise(feats(rsi_14=None, atr_14=None, adx_14=None, sma={}, bb_percent=None,
                             prev_close=None, adv20_inr=None))  # fmt: skip
    assert sparse == {}
    assert verbalise(feats(rsi_14=25.0, close=1050.0, prev_close=1000.0))["last_session"] == (
        "sharp rise last session"
    )


def event(
    days_ago_date: date, *, eid: str, kind=AnnouncementType.RESULTS_DATE, **kw: Any
) -> TypedEvent:
    published = datetime.combine(days_ago_date, time(10), IST)
    return TypedEvent(event_id=eid, instrument_key=INFY.key, published_at=published,
                      title=f"announcement {eid}", source="nse_rss", relevant=True,
                      announcement_type=kind, model="rules", calibrated=False,
                      classified_at=published, extra=kw)  # fmt: skip


async def proposal() -> tuple[Proposal, Features]:
    p, f = await one_proposal(None)
    return replace(p, intent=p.intent.model_copy(update={"instrument": INFY})), f


async def test_the_state_holds_public_facts_only():
    p, f = await proposal()
    state = veto_state(p, f, Regime.TRENDING_DOWN, [event(date(2026, 10, 1), eid="e1",
                                                          event_date="2026-10-16")])  # fmt: skip
    assert set(state) == {"instrument", "proposal", "market_regime", "features", "events"}
    assert state["instrument"] == "Infosys Ltd. (INFY, Information Technology)"
    assert state["market_regime"] == "trending down" and "buy position" in state["proposal"].lower()
    assert "results date" in state["events"] and "scheduled 2026-10-16" in state["events"]
    hints = typing.get_type_hints(veto_state)
    assert set(hints) - {"return"} == {"proposal", "features", "regime", "events"}
    text = " ".join(state.values()).lower()
    assert not {"cash", "equity", "pnl", "position size", "quantity"} & set(text.split())


class Model:
    name, checkpoint, context_tokens, device = "laya", "multilingual", 1024, "cpu"

    def __init__(self, p_veto: float | None, concern: str = "event_risk", confidence: float = 0.9):
        self.p_veto, self.concern, self.confidence = p_veto, concern, confidence
        self.states: list[Mapping[str, str]] = []

    async def decide(self, state: Mapping[str, str], questions: Mapping[str, Question]):
        self.states.append(dict(state))
        out = {"concern": Answer("choice", self.concern, {}, self.confidence, model="laya:m")}
        if self.p_veto is not None:
            out["veto"] = Answer("noul", self.p_veto >= 0.5,
                                 {"false": 1 - self.p_veto, "true": self.p_veto},
                                 self.confidence, model="laya:multilingual", calibrated=True)  # fmt: skip
        return out


def advisor(model: Model, events: list[TypedEvent] | None = None, threshold: float = 0.6):
    clock = ReplayClock(NOW)
    sink = RecordingSink(clock)
    cascade = Cascade(laya=model, jev=None, sink=sink, config=CascadeConfig(shadow_pct=0))
    adv = TypedVetoAdvisor(cascade=cascade, sink=sink, clock=clock, calendar=get_calendar(),
                           book_id="B", events=lambda key: list(events or []),
                           regime=lambda: Regime.RANGING, threshold=threshold)  # fmt: skip
    return adv, sink


@pytest.mark.parametrize(("p_veto", "expected"), [(0.8, Verdict.VETO), (0.6, Verdict.VETO),
                                                   (0.2, Verdict.APPROVE)])  # fmt: skip
async def test_veto_needs_a_calibrated_probability_at_the_threshold(p_veto, expected):
    adv, sink = advisor(Model(p_veto))
    p, f = await proposal()
    assert await adv.review(p, f) is expected
    verdict = [e.payload for e in sink.events if e.type == "AdvisorVerdict"][-1]
    assert verdict.book_id == "B" and verdict.confidence == p_veto
    assert [r.evidence_ref for r in verdict.reasons] == ["concern:event_risk"]


async def test_an_unsure_or_missing_answer_abstains():
    adv, sink = advisor(Model(0.5, confidence=0.5))  # inside the escalation band, no Jev
    p, f = await proposal()
    assert await adv.review(p, f) is Verdict.ABSTAIN
    (fallback,) = [e.payload for e in sink.events if e.type == "AdvisorFallback"]
    assert fallback.reason == "veto_jev_not_configured"
    adv2, _ = advisor(Model(None))
    assert await adv2.review(p, f) is Verdict.ABSTAIN


async def test_only_the_last_ten_sessions_of_published_events_are_seen():
    model = Model(0.1)
    events = [
        event(date(2026, 9, 18), eid="old"),  # the 11th most recent session (2 Oct holiday): out
        event(date(2026, 9, 21), eid="in_window"),  # the 10th most recent session: in
        event(date(2026, 10, 5), eid="future"),  # published 10:00 IST, after 09:30 now: out
    ]
    adv, _ = advisor(model, events)
    p, f = await proposal()
    await adv.review(p, f)
    seen = model.states[0]["events"]
    assert "announcement in_window" in seen and "old" not in seen and "future" not in seen


def test_questions_and_threshold_bounds():
    assert QUESTIONS["veto"].type == "noul" and QUESTIONS["concern"].cardinality == 6
    with pytest.raises(ValueError):
        advisor(Model(0.1), threshold=0)


def test_policy_and_negative_direction_render():
    e = event(date(2026, 10, 1), eid="x", kind=AnnouncementType.LITIGATION_OR_REGULATORY)
    e = e.model_copy(
        update={"direction": EventDirection.NEGATIVE, "materiality": Materiality.MAJOR}
    )
    from src.decision.advisors.typed_veto import describe_event

    assert describe_event(e).startswith("2026-10-01 | litigation or regulatory | negative | major")
    assert TradePolicy().config.k_stop_atr == 2.0 and Decimal(1) > 0
