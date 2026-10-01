"""Plan M7.1/7.2: typed questions, answers, and the Laya/Jev adapters (fakes + wire fixtures)."""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx2
import pytest

from src.decision_models.adapters import JEV_MODEL, LayaLocal, jev_remote, laya_http
from src.decision_models.base import (
    Answer,
    DecisionModelError,
    Question,
    estimate_tokens,
    parse_answers,
)

STATE = {"instrument": "Infosys Limited", "post": "Outcome of Board Meeting: results approved"}
QUESTIONS = {
    "relevant": Question("noul", "Is this relevant to the share price?"),
    "direction": Question(
        "choice",
        "Direction?",
        {"positive": "good", "negative": "bad", "neutral": "none", "unclear": "?"},
    ),  # fmt: skip
    "materiality": Question("score", "How material?", ("minor", "moderate", "major")),
}
REPLY = {"answers": {
    "relevant": {"type": "noul", "noul": 0.82, "answer_confidence": 0.79},
    "direction": {"type": "choice", "choice": "positive", "confidence": 0.6,
                  "probabilities": {"positive": 0.7, "negative": 0.1, "neutral": 0.15,
                                    "unclear": 0.05}},
    "materiality": {"type": "score", "score": 1.4,
                    "probabilities": {"minor": 0.2, "moderate": 0.5, "major": 0.3}},
}}  # fmt: skip


def test_questions_validate_and_render_the_wire_shape():
    assert QUESTIONS["relevant"].wire()["criteria"] == {"false": "no", "true": "yes"}
    assert QUESTIONS["materiality"].wire()["criteria"] == ["minor", "moderate", "major"]
    assert QUESTIONS["direction"].cardinality == 4
    with pytest.raises(ValueError):
        Question("choice", "x", {"only": "one"})
    with pytest.raises(ValueError):
        Question("score", "x", ("single",))


def test_answers_parse_with_calibrated_confidence_first():
    answers = parse_answers(REPLY, QUESTIONS, "laya:english")
    relevant = answers["relevant"]
    assert relevant.value is True and relevant.p_true == pytest.approx(0.82)
    assert relevant.confidence == 0.79  # answer_confidence wins over entropy confidence
    assert answers["direction"].value == "positive" and answers["direction"].confidence == 0.6
    assert answers["materiality"].value == 1.4 and answers["materiality"].confidence == 0.5
    assert answers["materiality"].p_true is None
    assert relevant.summary()["probabilities"] == {"false": 0.18, "true": 0.82}


@pytest.mark.parametrize(
    "payload",
    [{}, {"answers": {}}, {"answers": {"relevant": {"type": "choice", "choice": "x"}}},
     {"answers": {"relevant": {"type": "noul"}}}],
)  # fmt: skip
def test_malformed_replies_are_errors(payload):
    with pytest.raises(DecisionModelError):
        parse_answers(payload, {"relevant": QUESTIONS["relevant"]}, "m")


def test_token_estimate_is_conservative():
    assert estimate_tokens({"post": "x" * 4000}) >= 1000


class FakeAgent:
    def __init__(self) -> None:
        self.threads: set[str] = set()
        self.calls: list[tuple[Any, Any]] = []

    def predict(self, state: Any, questions: Any) -> dict[str, Any]:
        self.threads.add(threading.current_thread().name)
        self.calls.append((state, questions))
        return REPLY


async def test_laya_local_runs_on_its_own_single_thread():
    agent = FakeAgent()
    laya = LayaLocal(checkpoint="english", agent=agent)
    answers = await laya.decide(STATE, QUESTIONS)
    await laya.decide(STATE, QUESTIONS)
    assert answers["direction"].model == "laya:english" and laya.context_tokens == 512
    assert len(agent.threads) == 1 and next(iter(agent.threads)).startswith("laya")
    assert agent.calls[0][1]["materiality"]["criteria"] == ["minor", "moderate", "major"]
    laya.close()


async def test_laya_errors_and_unknown_checkpoints():
    class Broken:
        def predict(self, *a: Any) -> Any:
            raise RuntimeError("tokenizer exploded")

    with pytest.raises(DecisionModelError, match="tokenizer"):
        await LayaLocal(agent=Broken()).decide(STATE, QUESTIONS)
    with pytest.raises(ValueError):
        LayaLocal(checkpoint="huge")


class Wire:
    def __init__(self, *responses: tuple[int, Any, dict[str, str]]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body, headers = self.responses.pop(0)
        return httpx2.Response(status, json=body, headers=headers)


async def test_jev_wire_shape_auth_and_retry_after():
    wire = Wire((429, {"error": "slow"}, {"retry-after": "7"}), (200, REPLY, {}))
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    jev = jev_remote("ts-test-key", client=httpx2.AsyncClient(transport=httpx2.MockTransport(wire)),
                     sleep=sleep)  # fmt: skip
    answers = await jev.decide(STATE, QUESTIONS)
    request = wire.requests[-1]
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer ts-test-key"
    body = json.loads(request.content)
    assert body["model"] == JEV_MODEL and body["state"] == STATE
    assert body["questions"]["relevant"]["type"] == "noul"
    assert slept == [7.0] and answers["relevant"].model == f"jev:{JEV_MODEL}"


async def test_jev_gives_up_after_three_rate_limits_and_caps_the_wait():
    wire = Wire(*[(529, {}, {"retry-after": "120"})] * 3)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    jev = jev_remote(
        "k", client=httpx2.AsyncClient(transport=httpx2.MockTransport(wire)), sleep=sleep
    )
    with pytest.raises(DecisionModelError, match="rate-limited"):
        await jev.decide(STATE, QUESTIONS)
    assert slept == [30.0, 30.0] and len(wire.requests) == 3


async def test_laya_serve_needs_no_key_and_errors_are_typed():
    ok = Wire((200, REPLY, {}))
    laya = laya_http("http://box:8000", checkpoint="multilingual",
                     client=httpx2.AsyncClient(transport=httpx2.MockTransport(ok)))  # fmt: skip
    answers = await laya.decide(STATE, QUESTIONS)
    assert "authorization" not in ok.requests[0].headers and laya.context_tokens == 1024
    assert isinstance(answers["relevant"], Answer)
    down = Wire((500, {"error": "x"}, {}))
    broken = laya_http(
        "http://box:8000", client=httpx2.AsyncClient(transport=httpx2.MockTransport(down))
    )
    with pytest.raises(DecisionModelError, match="HTTP 500"):
        await broken.decide(STATE, QUESTIONS)
