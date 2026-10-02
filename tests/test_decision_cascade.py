"""Plan M7.3: the Laya→Jev cascade - escalation band, cardinality, oversize state, Laya error,
Jev down → ABSTAIN, shadow sampling, and a DecisionModelCall per call."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from src.decision_models.base import Answer, DecisionModelError, Question
from src.decision_models.cascade import Cascade, CascadeConfig
from src.domain.clock import ReplayClock
from src.domain.sink import RecordingSink

STATE = {"instrument": "Infosys Limited", "post": "Board approves interim dividend"}
NOUL = Question("noul", "Relevant?")
DIRECTION = Question("choice", "Direction?", {"positive": "+", "negative": "-", "neutral": "0"})
WIDE = Question("choice", "Which sector?", {f"s{i}": str(i) for i in range(25)})


class Fake:
    def __init__(self, name: str, confidences: Mapping[str, float] | None = None, *,
                 fail: bool = False, context: int | None = 512) -> None:  # fmt: skip
        self.name = name
        self.checkpoint = f"{name}-ckpt"
        self.context_tokens = context
        self.confidences = dict(confidences or {})
        self.fail = fail
        self.asked: list[list[str]] = []

    async def decide(self, state: Mapping[str, str], questions: Mapping[str, Question]):
        self.asked.append(sorted(questions))
        if self.fail:
            raise DecisionModelError(f"{self.name} down")
        out = {}
        for name, q in questions.items():
            conf = self.confidences.get(name, 0.9)
            value: Any = True if q.type == "noul" else next(iter(q.criteria))  # type: ignore[arg-type]
            out[name] = Answer(q.type, value, {"true": conf} if q.type == "noul" else {}, conf,
                               model=self.name)  # fmt: skip
        return out


def cascade(laya: Fake | None, jev: Fake | None, **config: Any) -> tuple[Cascade, RecordingSink]:
    sink = RecordingSink(ReplayClock(datetime(2026, 10, 5, 4, tzinfo=UTC)))
    cfg = CascadeConfig(**({"shadow_pct": 0.0} | config))
    return Cascade(laya=laya, jev=jev, sink=sink, config=cfg), sink


def calls(sink: RecordingSink) -> list[tuple[str, bool, bool, str | None]]:
    return [(e.payload.model, e.payload.escalated, e.payload.shadow, e.payload.escalation_reason)
            for e in sink.events]  # fmt: skip


async def test_confident_laya_answers_stand_alone():
    laya, jev = Fake("laya"), Fake("jev")
    c, sink = cascade(laya, jev)
    result = await c.decide("announcements", STATE, {"relevant": NOUL, "direction": DIRECTION})
    assert set(result.answers) == {"relevant", "direction"} and not result.escalated
    assert jev.asked == [] and calls(sink) == [("laya", False, False, None)]


async def test_only_the_uncertain_questions_escalate():
    laya, jev = Fake("laya", {"direction": 0.5}), Fake("jev")
    c, sink = cascade(laya, jev)
    result = await c.decide("announcements", STATE, {"relevant": NOUL, "direction": DIRECTION})
    assert result.escalated == {"direction": "uncertain"} and jev.asked == [["direction"]]
    assert result.answers["direction"].model == "jev" and result.answers["relevant"].model == "laya"
    assert calls(sink)[-1] == ("jev", True, False, "uncertain")


async def test_band_edges_are_inclusive_and_configurable():
    laya, jev = Fake("laya", {"relevant": 0.65}), Fake("jev")
    c, _ = cascade(laya, jev)
    assert (await c.decide("t", STATE, {"relevant": NOUL})).escalated
    c2, _ = cascade(Fake("laya", {"relevant": 0.65}), Fake("jev"), escalate_band=(0.4, 0.6))
    assert not (await c2.decide("t", STATE, {"relevant": NOUL})).escalated


async def test_high_cardinality_goes_to_jev():
    laya, jev = Fake("laya"), Fake("jev")
    c, _ = cascade(laya, jev)
    result = await c.decide("t", STATE, {"sector": WIDE})
    assert (
        result.escalated == {"sector": "high_cardinality"}
        and result.answers["sector"].model == "jev"
    )


async def test_an_oversize_state_skips_laya_entirely():
    laya, jev = Fake("laya", context=16), Fake("jev")
    c, sink = cascade(laya, jev)
    result = await c.decide("t", {"post": "x" * 400}, {"relevant": NOUL})
    assert laya.asked == [] and result.escalated == {"relevant": "state_exceeds_context"}
    assert calls(sink) == [("jev", True, False, "state_exceeds_context")]


async def test_a_laya_error_escalates_everything():
    c, sink = cascade(Fake("laya", fail=True), Fake("jev"))
    result = await c.decide("t", STATE, {"relevant": NOUL, "direction": DIRECTION})
    assert result.escalated == {"relevant": "laya_error", "direction": "laya_error"}
    assert set(result.answers) == {"relevant", "direction"}
    laya_call = sink.events[0].payload
    assert laya_call.outcome.value == "error" and "laya down" in laya_call.error


@pytest.mark.parametrize("jev", [None, Fake("jev", fail=True)], ids=["not_configured", "down"])
async def test_jev_unavailable_means_abstain_never_the_unsure_answer(jev):
    c, _ = cascade(Fake("laya", {"direction": 0.5}), jev)
    result = await c.decide("t", STATE, {"relevant": NOUL, "direction": DIRECTION})
    assert "direction" not in result.answers and "relevant" in result.answers
    assert result.abstained == {
        "direction": "jev_not_configured" if jev is None else "jev_unavailable"
    }


async def test_shadow_sampling_is_deterministic_and_recorded_only():
    laya, jev = Fake("laya"), Fake("jev")
    c, sink = cascade(laya, jev, shadow_pct=1.0)
    result = await c.decide("t", STATE, {"relevant": NOUL})
    assert result.shadow and result.answers["relevant"].model == "laya"  # Jev's answer unused
    assert calls(sink) == [("laya", False, False, None), ("jev", False, True, None)]
    none, _ = cascade(Fake("laya"), Fake("jev"), shadow_pct=0.0)
    assert not (await none.decide("t", STATE, {"relevant": NOUL})).shadow
    half, _ = cascade(Fake("laya"), Fake("jev"), shadow_pct=0.5)
    picks = [await half.decide("t", {"post": f"n{i}"}, {"relevant": NOUL}) for i in range(200)]
    share = sum(p.shadow for p in picks) / 200
    assert 0.35 < share < 0.65
    again = [await half.decide("t", {"post": f"n{i}"}, {"relevant": NOUL}) for i in range(20)]
    assert [p.shadow for p in again] == [p.shadow for p in picks[:20]]  # repeatable


def test_config_bounds():
    with pytest.raises(ValueError):
        CascadeConfig(escalate_band=(0.7, 0.3))
    with pytest.raises(ValueError):
        CascadeConfig(shadow_pct=2)
    with pytest.raises(ValueError):
        Cascade(
            laya=None, jev=None, sink=RecordingSink(ReplayClock(datetime(2026, 1, 1, tzinfo=UTC)))
        )
