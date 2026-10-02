"""
The decision-model cascade (plan M7.3): Laya first, Jev only when Laya should not be trusted.

Per state, Laya answers every question. A question is **escalated** to Jev when:

(a) its calibrated confidence falls inside ``escalate_band`` (default 0.35-0.65: Laya is unsure);
(b) it is a ``choice`` with more than ``max_choices`` labels (Laya is weak at high cardinality);
(c) the state is longer than the Laya checkpoint's context (every question); or
(d) Laya failed (every question).

Escalated questions take Jev's answer; when Jev is not configured or fails, they are **ABSTAIN**
(``CascadeResult.abstained``) - an unsure Laya answer is never used as if it were sure.

**Shadow sampling:** a deterministic ``shadow_pct`` of Laya-only states also go to Jev, recorded
but not used, to measure agreement. Every call is a ``DecisionModelCall`` event.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from src.decision_models.base import (
    Answer,
    DecisionModel,
    DecisionModelError,
    Question,
    estimate_tokens,
)
from src.domain.events import DecisionModelCall, LLMOutcome
from src.domain.sink import EventSink

logger = logging.getLogger(__name__)


class Calibrator(Protocol):
    def apply(self, task: str, question: str, answer: Answer) -> Answer:
        """The answer with its probabilities/confidence calibrated (unchanged when unknown)."""
        ...


class Identity:
    def apply(self, task: str, question: str, answer: Answer) -> Answer:
        return answer


@dataclass(frozen=True)
class CascadeConfig:
    escalate_band: tuple[float, float] = (0.35, 0.65)
    max_choices: int = 20
    shadow_pct: float = 0.20

    def __post_init__(self) -> None:
        low, high = self.escalate_band
        if not 0 <= low <= high <= 1:
            raise ValueError("escalate_band must satisfy 0 <= low <= high <= 1")
        if not 0 <= self.shadow_pct <= 1:
            raise ValueError("shadow_pct must be in [0, 1]")


@dataclass
class CascadeResult:
    answers: dict[str, Answer] = field(default_factory=dict)
    abstained: dict[str, str] = field(default_factory=dict)  # question -> why
    escalated: dict[str, str] = field(default_factory=dict)  # question -> why it went to Jev
    shadow: bool = False


class Cascade:
    def __init__(
        self,
        *,
        laya: DecisionModel | None,
        jev: DecisionModel | None,
        sink: EventSink,
        config: CascadeConfig | None = None,
        calibrator: Calibrator | None = None,
    ) -> None:
        if laya is None and jev is None:
            raise ValueError("the cascade needs at least one decision model")
        self._laya = laya
        self._jev = jev
        self._sink = sink
        self.config = config or CascadeConfig()
        self._calibrator = calibrator or Identity()

    async def decide(
        self,
        task: str,
        state: Mapping[str, str],
        questions: Mapping[str, Question],
        *,
        decision_id: str | None = None,
        book_id: str | None = None,
    ) -> CascadeResult:
        result = CascadeResult()
        escalate: dict[str, str] = {}
        laya_answers: dict[str, Answer] = {}
        if self._laya is None:
            escalate = dict.fromkeys(questions, "no_laya")
        else:
            limit = self._laya.context_tokens
            if limit is not None and estimate_tokens(state) > limit:
                escalate = dict.fromkeys(questions, "state_exceeds_context")
            else:
                laya_answers, error = await self._call(self._laya, task, state, questions,
                                                       decision_id, book_id)  # fmt: skip
                if error is not None:
                    escalate = dict.fromkeys(questions, "laya_error")
                else:
                    escalate = self._uncertain(task, questions, laya_answers)
        for name, answer in laya_answers.items():
            if name not in escalate:
                result.answers[name] = answer

        if escalate:
            result.escalated = escalate
            asked = {n: questions[n] for n in escalate}
            jev_answers: dict[str, Answer] = {}
            reason = "jev_not_configured"
            if self._jev is not None:
                jev_answers, error = await self._call(
                    self._jev, task, state, asked, decision_id, book_id,
                    escalation=", ".join(sorted(set(escalate.values()))),
                )  # fmt: skip
                reason = "jev_unavailable" if error is not None else reason
            for name in asked:
                if name in jev_answers:
                    result.answers[name] = jev_answers[name]
                else:
                    result.abstained[name] = reason
        elif self._jev is not None and self._sampled(task, state, questions):
            result.shadow = True
            await self._call(self._jev, task, state, questions, decision_id, book_id, shadow=True)
        return result

    def _uncertain(
        self, task: str, questions: Mapping[str, Question], answers: Mapping[str, Answer]
    ) -> dict[str, str]:
        low, high = self.config.escalate_band
        out: dict[str, str] = {}
        for name, question in questions.items():
            if question.type == "choice" and question.cardinality > self.config.max_choices:
                out[name] = "high_cardinality"
                continue
            answer = answers.get(name)
            if answer is None:
                out[name] = "laya_error"
            elif low <= answer.confidence <= high:
                out[name] = "uncertain"
        return out

    def _sampled(
        self, task: str, state: Mapping[str, str], questions: Mapping[str, Question]
    ) -> bool:
        if self.config.shadow_pct <= 0:
            return False
        key = json.dumps([task, dict(state), sorted(questions)], sort_keys=True)
        bucket = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 10_000
        return bucket < self.config.shadow_pct * 10_000

    async def _call(
        self,
        model: DecisionModel,
        task: str,
        state: Mapping[str, str],
        questions: Mapping[str, Question],
        decision_id: str | None,
        book_id: str | None,
        *,
        escalation: str | None = None,
        shadow: bool = False,
    ) -> tuple[dict[str, Answer], str | None]:
        started = time.perf_counter()
        answers: dict[str, Answer] = {}
        error: str | None = None
        try:
            raw = await model.decide(state, questions)
            answers = {n: self._calibrator.apply(task, n, a) for n, a in raw.items()}
        except DecisionModelError as exc:
            error = str(exc)
        except Exception as exc:  # a model bug never escapes the cascade
            logger.exception("decision model %s failed", model.name)
            error = f"{type(exc).__name__}: {exc}"
        self._sink.emit(
            DecisionModelCall(
                decision_id=decision_id, book_id=book_id, task=task, model=model.name,
                checkpoint=model.checkpoint, device=getattr(model, "device", None),
                question_count=len(questions),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                escalated=escalation is not None, escalation_reason=escalation, shadow=shadow,
                calibrated=any(a.calibrated for a in answers.values()),
                answers={n: a.summary() for n, a in answers.items()},
                outcome=LLMOutcome.OK if error is None else LLMOutcome.ERROR, error=error,
            ),
            source="decision_models",
        )  # fmt: skip
        return answers, error
