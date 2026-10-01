"""
Typed decision models (plan M7.1; decision D7): Laya (local, open) and Jev (TypeSafe, remote)
answer typed questions about a **state** with calibrated probabilities.

* :class:`Question` - ``noul`` (yes/no), ``choice`` (one of ≤255 labels) or ``score`` (an ordinal
  scale), with instructions and criteria; :meth:`Question.wire` is the shape both Laya and Jev
  accept (``laya-serve`` speaks Jev's ``/v1/systemone``).
* :class:`Answer` - the value, the per-label probabilities, and ``confidence``: the model's
  calibrated probability that the reported value is right (Laya's ``answer_confidence``; else
  the reported entropy confidence; else the top probability).
* :class:`DecisionModel` - ``await decide(state, questions) -> {name: Answer}``.

A state is a small mapping of **public** text (instrument, announcement, verbalised features) -
never portfolio data (plan M7 acceptance; enforced by the task state builders).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

QuestionType = Literal["noul", "choice", "score"]
MAX_CHOICES = 255


class DecisionModelError(Exception):
    """The model could not answer (not loaded, unreachable, malformed reply)."""


@dataclass(frozen=True)
class Question:
    type: QuestionType
    instructions: str
    criteria: Mapping[str, str] | tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.type == "choice":
            if not isinstance(self.criteria, Mapping) or not 2 <= len(self.criteria) <= MAX_CHOICES:
                raise ValueError(f"a choice question needs 2..{MAX_CHOICES} labelled criteria")
        if self.type == "score" and (
            not isinstance(self.criteria, tuple) or len(self.criteria) < 2
        ):
            raise ValueError("a score question needs an ordered tuple of at least 2 levels")

    @property
    def cardinality(self) -> int:
        return len(self.criteria) if self.criteria is not None else 2

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = (
                dict(self.criteria) if isinstance(self.criteria, Mapping) else list(self.criteria)
            )
        elif self.type == "noul":
            out["criteria"] = {"false": "no", "true": "yes"}
        return out


@dataclass(frozen=True)
class Answer:
    type: QuestionType
    value: bool | str | float  # noul: P(true) >= 0.5; choice: the label; score: expected level
    probabilities: Mapping[str, float]
    confidence: float
    model: str = ""
    calibrated: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def p_true(self) -> float | None:
        """For a noul answer, the probability of "true"."""
        if self.type != "noul":
            return None
        return float(self.probabilities.get("true", 1.0 if self.value is True else 0.0))

    def summary(self) -> dict[str, Any]:
        """JSON-ready, for ``DecisionModelCall`` events."""
        return {"value": self.value, "confidence": round(self.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()}}  # fmt: skip


class DecisionModel(Protocol):
    @property
    def name(self) -> str:
        """``"laya"`` or ``"jev"``."""
        ...

    @property
    def checkpoint(self) -> str: ...

    @property
    def context_tokens(self) -> int | None:
        """The longest state it reads (None = large enough not to matter)."""
        ...

    async def decide(
        self, state: Mapping[str, str], questions: Mapping[str, Question]
    ) -> dict[str, Answer]: ...


def parse_answer(qtype: QuestionType, raw: Mapping[str, Any], model: str) -> Answer:
    """One answer from the Laya/Jev wire shape (``noul``/``choice``/``score`` + probabilities)."""
    probs = {str(k): float(v) for k, v in dict(raw.get("probabilities") or {}).items()}
    value: bool | str | float
    if qtype == "noul":
        if "noul" not in raw:
            raise DecisionModelError("noul answer without 'noul'")
        p_true = float(raw["noul"])
        probs = probs or {"false": 1.0 - p_true, "true": p_true}
        value = p_true >= 0.5
    elif qtype == "choice":
        if "choice" not in raw:
            raise DecisionModelError("choice answer without 'choice'")
        value = str(raw["choice"])
    else:
        if "score" not in raw:
            raise DecisionModelError("score answer without 'score'")
        value = float(raw["score"])
    confidence = raw.get("answer_confidence")
    if confidence is None:
        confidence = raw.get("confidence")
    if confidence is None:
        confidence = max(probs.values()) if probs else 0.0
    return Answer(qtype, value, probs, float(confidence), model, raw=dict(raw))


def parse_answers(
    payload: Mapping[str, Any], questions: Mapping[str, Question], model: str
) -> dict[str, Answer]:
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise DecisionModelError("reply has no 'answers'")
    out: dict[str, Answer] = {}
    for name, question in questions.items():
        raw = answers.get(name)
        if not isinstance(raw, Mapping):
            raise DecisionModelError(f"no answer for {name!r}")
        reported = raw.get("type")
        if reported is not None and reported != question.type:
            raise DecisionModelError(f"{name!r}: asked {question.type}, got {reported}")
        out[name] = parse_answer(question.type, raw, model)
    return out


def estimate_tokens(state: Mapping[str, str]) -> int:
    """A conservative token estimate (≈4 characters per token, plus labels)."""
    return sum(len(k) + len(v) for k, v in state.items()) // 4 + 8 * len(state)
