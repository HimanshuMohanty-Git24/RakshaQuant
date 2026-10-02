"""
Calibration of decision-model answers (plan M7.5).

Laya's checkpoints ship over-confident (and, for 11+-label choices, with invalid temperatures),
so their probabilities are rescaled per (model, task.question) by **temperature scaling** fitted
on labelled data: ``p' = softmax(log p / T)``. ``T > 1`` softens an over-confident model. The
argmax - the reported answer - never changes; only how sure it claims to be.

* :func:`fit_temperature` - the ``T`` minimising the held-in negative log-likelihood.
* :func:`report` - accuracy, Brier score, ECE and reliability bins.
* :class:`CalibrationMap` - the fitted temperatures (``var/models/calibration.json``); it is the
  cascade's ``Calibrator``: answers are calibrated before the escalation band is applied.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from src.decision_models.base import Answer

EPS = 1e-6
ProbsLike = Sequence[Sequence[float]] | np.ndarray
LabelsLike = Sequence[int] | np.ndarray
T_MIN, T_MAX = 0.05, 20.0


def _as_matrix(probs: ProbsLike) -> np.ndarray:
    matrix = np.clip(np.asarray(probs, dtype=float), EPS, 1.0)
    return np.asarray(matrix / matrix.sum(axis=1, keepdims=True))


def apply_temperature(probs: ProbsLike, temperature: float) -> np.ndarray:
    logits = np.log(_as_matrix(probs)) / temperature
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return np.asarray(exp / exp.sum(axis=1, keepdims=True))


def nll(probs: ProbsLike, labels: LabelsLike, temperature: float = 1.0) -> float:
    scaled = apply_temperature(probs, temperature)
    picked = scaled[np.arange(len(labels)), np.asarray(labels)]
    return float(-np.mean(np.log(np.clip(picked, EPS, 1.0))))


def fit_temperature(probs: ProbsLike, labels: LabelsLike) -> float:
    """Golden-section search on log T over [0.05, 20] (NLL is unimodal in T)."""
    if len(labels) == 0:
        return 1.0
    lo, hi = math.log(T_MIN), math.log(T_MAX)
    ratio = (math.sqrt(5) - 1) / 2
    a, b = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
    fa, fb = nll(probs, labels, math.exp(a)), nll(probs, labels, math.exp(b))
    for _ in range(80):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - ratio * (hi - lo)
            fa = nll(probs, labels, math.exp(a))
        else:
            lo, a, fa = a, b, fb
            b = lo + ratio * (hi - lo)
            fb = nll(probs, labels, math.exp(b))
    return round(math.exp((lo + hi) / 2), 4)


@dataclass(frozen=True)
class Report:
    n: int
    accuracy: float
    brier: float
    ece: float
    nll: float
    bins: list[dict[str, float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"n": self.n, "accuracy": round(self.accuracy, 4), "brier": round(self.brier, 4),
                "ece": round(self.ece, 4), "nll": round(self.nll, 4), "bins": self.bins}  # fmt: skip


def report(probs: ProbsLike, labels: LabelsLike, n_bins: int = 10) -> Report:
    """Accuracy, multi-class Brier (mean squared error to the one-hot label), ECE and
    reliability bins (on the top-class confidence)."""
    y = np.asarray(labels)
    n = len(y)
    if n == 0:
        return Report(0, 0.0, 0.0, 0.0, 0.0)
    p = _as_matrix(probs)
    onehot = np.zeros_like(p)
    onehot[np.arange(n), y] = 1.0
    confidence = p.max(axis=1)
    correct = (p.argmax(axis=1) == y).astype(float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[dict[str, float]] = []
    ece = 0.0
    for i in range(n_bins):
        mask = (confidence > edges[i]) & (confidence <= edges[i + 1])
        if i == 0:
            mask |= confidence == 0.0
        count = int(mask.sum())
        if count == 0:
            continue
        acc, conf = float(correct[mask].mean()), float(confidence[mask].mean())
        ece += abs(acc - conf) * count / n
        bins.append({"lo": round(float(edges[i]), 2), "hi": round(float(edges[i + 1]), 2),
                     "count": count, "accuracy": round(acc, 4), "confidence": round(conf, 4)})  # fmt: skip
    return Report(
        n=n,
        accuracy=float(correct.mean()),
        brier=float(np.mean(np.sum((p - onehot) ** 2, axis=1))),
        ece=float(ece),
        nll=nll(p, y),
        bins=bins,
    )


def key(model: str, task: str, question: str) -> str:
    return f"{model}|{task}.{question}"


class CalibrationMap:
    """Fitted temperatures by ``model|task.question``; applies them to answers."""

    def __init__(self, temperatures: Mapping[str, float] | None = None) -> None:
        self.temperatures = dict(temperatures or {})

    @classmethod
    def load(cls, path: Path) -> CalibrationMap:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls({str(k): float(v) for k, v in dict(data.get("temperatures", {})).items()})

    def save(self, path: Path, *, meta: Mapping[str, Any] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"temperatures": dict(sorted(self.temperatures.items())), "meta": dict(meta or {})}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def apply(self, task: str, question: str, answer: Answer) -> Answer:
        temperature = self.temperatures.get(key(answer.model, task, question))
        if temperature is None or not answer.probabilities:
            return answer
        labels = list(answer.probabilities)
        scaled = apply_temperature([[answer.probabilities[k] for k in labels]], temperature)[0]
        probs = {k: float(v) for k, v in zip(labels, scaled, strict=True)}
        if answer.type == "noul":
            p_true = probs.get("true", 0.0)
            value: bool | str | float = p_true >= 0.5
            confidence = p_true if value else 1.0 - p_true
        elif answer.type == "choice":
            value = answer.value
            confidence = probs.get(str(answer.value), max(probs.values()))
        else:  # score: the expected level index under the calibrated distribution
            value = float(sum(i * probs[k] for i, k in enumerate(labels)))
            confidence = max(probs.values())
        return replace(answer, value=value, probabilities=probs, confidence=float(confidence),
                       calibrated=True)  # fmt: skip


@dataclass(frozen=True)
class QuestionResult:
    question: str
    temperature: float
    train_n: int
    before: Report  # held-out, uncalibrated
    after: Report  # held-out, calibrated with the train-fitted temperature

    def as_dict(self) -> dict[str, Any]:
        return {"question": self.question, "temperature": self.temperature,
                "train_n": self.train_n, "heldout_before": self.before.as_dict(),
                "heldout_after": self.after.as_dict()}  # fmt: skip


def calibrate(
    rows: Sequence[tuple[str, Sequence[float], int]],
    question: str,
    heldout: Sequence[bool],
) -> QuestionResult:
    """Fit a temperature on the training rows and score it on the held-out rows."""
    train = [(p, y) for (_, p, y), h in zip(rows, heldout, strict=True) if not h]
    test = [(p, y) for (_, p, y), h in zip(rows, heldout, strict=True) if h]
    temperature = fit_temperature([p for p, _ in train], [y for _, y in train]) if train else 1.0
    test_p, test_y = [p for p, _ in test], [y for _, y in test]
    scaled = apply_temperature(test_p, temperature) if test else test_p
    return QuestionResult(question, temperature, len(train), report(test_p, test_y),
                          report(scaled, test_y))  # fmt: skip
