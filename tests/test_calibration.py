"""Plan M7.5: temperature scaling, metrics, the labelled set, and the scripts (no paid calls)."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.decision_models import calibration, labels
from src.decision_models.base import Answer, Question
from src.decision_models.calibration import (
    CalibrationMap,
    apply_temperature,
    fit_temperature,
    report,
)
from src.domain.events import AnnouncementReceived
from src.domain.types import Instrument
from src.llm.pricing import PricingTable
from src.llm.prompts.templates import AnnouncementLabel
from src.llm.registry import ModelSpec
from src.llm.router import LLMResult

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 5, 4, tzinfo=UTC)


def overconfident(n: int, k: int = 3, seed: int = 0, accuracy: float = 0.6):
    """A model that is right ``accuracy`` of the time but always ~97% sure."""
    rng = np.random.default_rng(seed)
    labels_ = rng.integers(0, k, n)
    picks = np.where(rng.random(n) < accuracy, labels_, (labels_ + 1) % k)
    probs = np.full((n, k), 0.03 / (k - 1))
    probs[np.arange(n), picks] = 0.97
    return probs, labels_


def test_temperature_softens_an_overconfident_model_and_improves_heldout_scores():
    train_p, train_y = overconfident(400, seed=1)
    test_p, test_y = overconfident(200, seed=2)
    t = fit_temperature(train_p, train_y)
    assert t > 1.5
    before, after = report(test_p, test_y), report(apply_temperature(test_p, t), test_y)
    assert after.brier < before.brier and after.ece < before.ece  # the acceptance criterion
    assert after.accuracy == before.accuracy  # the argmax never changes
    assert sum(b["count"] for b in after.bins) == 200


def test_a_calibrated_model_keeps_a_temperature_near_one():
    rng = np.random.default_rng(3)
    p_true = rng.uniform(0.05, 0.95, 3000)
    y = (rng.random(3000) < p_true).astype(int)
    assert 0.8 < fit_temperature(np.stack([1 - p_true, p_true], axis=1), y) < 1.25


def test_metrics_on_known_values():
    r = report([[1.0, 0.0], [0.0, 1.0]], [0, 0])
    assert (
        r.accuracy == 0.5
        and r.brier == pytest.approx(1.0, abs=1e-4)
        and r.ece == pytest.approx(0.5, abs=1e-3)
    )
    assert report([], []).n == 0 and fit_temperature([], []) == 1.0


def test_the_map_applies_per_model_and_question_and_round_trips(tmp_path):
    cmap = CalibrationMap({calibration.key("laya:multilingual", "announcements", "relevant"): 3.0,
                           calibration.key("laya:multilingual", "announcements", "direction"): 2.0,
                           calibration.key("laya:multilingual", "announcements", "materiality"): 2.0})  # fmt: skip
    noul = Answer("noul", True, {"false": 0.05, "true": 0.95}, 0.95, model="laya:multilingual")
    out = cmap.apply("announcements", "relevant", noul)
    assert out.calibrated and out.value is True and 0.5 < out.p_true < 0.95  # type: ignore[operator]
    assert out.confidence == pytest.approx(out.p_true)
    choice = Answer("choice", "negative", {"positive": 0.02, "negative": 0.9, "neutral": 0.05,
                                           "unclear": 0.03}, 0.9, model="laya:multilingual")  # fmt: skip
    c = cmap.apply("announcements", "direction", choice)
    assert c.value == "negative" and c.confidence < 0.9
    score = Answer("score", 1.9, {"minor": 0.05, "moderate": 0.1, "major": 0.85}, 0.85,
                   model="laya:multilingual")  # fmt: skip
    sc = cmap.apply("announcements", "materiality", score)
    assert sc.value < 1.9 and max(sc.probabilities, key=sc.probabilities.get) == "major"  # type: ignore[operator, arg-type]
    other = cmap.apply(
        "announcements", "relevant", noul.__class__(**{**noul.__dict__, "model": "jev:x"})
    )
    assert not other.calibrated  # no temperature for that model
    cmap.save(tmp_path / "calibration.json", meta={"best": "laya:multilingual"})
    assert CalibrationMap.load(tmp_path / "calibration.json").temperatures == cmap.temperatures
    assert CalibrationMap.load(tmp_path / "missing.json").temperatures == {}


# --- the labelled set ---------------------------------------------------------------------------

UNIVERSE = [Instrument.nse_equity("INFY", name="Infosys Ltd."),
            Instrument.nse_equity("TCS", name="Tata Consultancy Services Ltd.")]  # fmt: skip


def stored(aid: str, text: str = "Board meeting to consider results") -> AnnouncementReceived:
    return AnnouncementReceived(announcement_id=aid, instrument_key="NSE:EQ:INFY",
                                company="Infosys Limited", published_at=T0, received_at=T0,
                                title=text, subject="Board Meeting Intimation", source="nse_rss")  # fmt: skip


def test_collect_from_the_store_and_an_nse_csv(tmp_path):
    csv = tmp_path / "CF-AN-equities.csv"
    pd.DataFrame({
        "SYMBOL": ["INFY", "TCS", "ZZZ", "TCS"],
        "COMPANY NAME": ["Infosys Limited", "Tata Consultancy Services Limited", "Other", "x"],
        "SUBJECT": ["Financial Result Updates", "Dividend", "Updates", "Bad date"],
        "DETAILS": ["Results for Q2", "Interim dividend of Rs 9", "n/a", "text"],
        "BROADCAST DATE/TIME": ["16-Oct-2026 17:05:10", "09-Oct-2026 18:00:00",
                                "01-Oct-2026 10:00:00", "not a date"],
    }).to_csv(csv, index=False)  # fmt: skip
    from_csv = labels.from_nse_csv(csv, UNIVERSE)
    assert sorted(from_csv["instrument_key"]) == ["NSE:EQ:INFY", "NSE:EQ:TCS"]
    combined = labels.merge(labels.from_announcements([stored("a1")]), from_csv, from_csv)
    assert len(combined) == 3 and combined["label_event_type"].isna().all()
    labels.save(combined, tmp_path / "set.parquet")
    assert labels.load(tmp_path / "set.parquet").equals(combined)
    with pytest.raises(ValueError, match="details"):
        bad = tmp_path / "bad.csv"
        pd.DataFrame({"SYMBOL": ["INFY"]}).to_csv(bad, index=False)
        labels.from_nse_csv(bad, UNIVERSE)


class FakeRouter:
    def __init__(self, fail_every: int = 0) -> None:
        self.calls, self.fail_every = 0, fail_every

    async def complete(self, role: str, messages: Any, schema: type, **kw: Any) -> LLMResult[Any]:
        self.calls += 1
        assert role == "label" and "<data" in messages[-1].content
        if self.fail_every and self.calls % self.fail_every == 0:
            return LLMResult(role, "timeout")
        label = AnnouncementLabel(relevant=True, event_type="results_date", direction="neutral",
                                  materiality="moderate", confidence=0.8, schema_version=1)  # fmt: skip
        return LLMResult(role, "ok", label, model="anthropic:claude-opus-5-5")


async def test_teacher_labels_cost_estimate_spotcheck_and_corrections():
    df = labels.from_announcements([stored(f"a{i}") for i in range(6)])
    calls, inr = labels.estimate_cost(df, ModelSpec.parse("anthropic:claude-opus-5-5"),
                                      PricingTable.from_yaml(), 88.0)  # fmt: skip
    assert calls == 6 and inr is not None and 0 < inr < 5
    router = FakeRouter(fail_every=3)
    labelled = await labels.teacher_label(df, router, limit=5)  # type: ignore[arg-type]
    assert router.calls == 5 and labelled["label_event_type"].notna().sum() == 4  # 1 timeout
    assert set(labelled["teacher_model"].dropna()) == {"anthropic:claude-opus-5-5"}

    sample = labels.spotcheck(labelled, n=50)
    assert len(sample) == 4 and "correct_event_type" in sample.columns
    sample.loc[0, "correct_event_type"] = "results"
    sample.loc[1, "correct_relevant"] = "false"
    fixed, changed = labels.merge_corrections(labelled, sample)
    assert changed == 2 and fixed["corrected"].sum() == 2
    row = fixed.set_index("announcement_id").loc[sample.loc[0, "announcement_id"]]
    assert row["label_event_type"] == "results"
    sample.loc[2, "correct_direction"] = "sideways"
    with pytest.raises(ValueError, match="direction"):
        labels.merge_corrections(labelled, sample)


class FakeModel:
    name, checkpoint, context_tokens = "laya", "multilingual", 1024

    async def decide(self, state: Mapping[str, str], questions: Mapping[str, Question]):
        return {
            "relevant": Answer("noul", True, {"false": 0.1, "true": 0.9}, 0.9, model="laya:m"),
            "event_type": Answer("choice", "results_date",
                                 {"results_date": 0.97, "results": 0.03}, 0.97, model="laya:m"),
            "direction": Answer("choice", "neutral", {"neutral": 0.95, "positive": 0.05}, 0.95,
                                model="laya:m"),
            "materiality": Answer("score", 1.0, {"0": 0.1, "1": 0.8, "2": 0.1}, 0.8, model="laya:m"),
        }  # fmt: skip


async def test_predictions_align_probabilities_to_the_label_order():
    df = labels.from_announcements([stored(f"a{i}") for i in range(4)])
    df = await labels.teacher_label(df, FakeRouter())  # type: ignore[arg-type]
    out = await labels.predict(FakeModel(), df)
    aid, probs, y = out["materiality"][0]
    assert probs == [0.1, 0.8, 0.1] and y == 1  # index-keyed score probabilities aligned
    assert out["relevant"][0][1] == [0.1, 0.9] and out["relevant"][0][2] == 1
    assert len(out["event_type"][0][1]) == 12
    assert labels.split(["a", "b"]) == labels.split(["a", "b"])  # deterministic


# --- the scripts --------------------------------------------------------------------------------


def load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_t", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_label_is_a_dry_run_without_confirmation(settings, monkeypatch, capsys):
    from pydantic import SecretStr

    script = load_script("build_decision_labels")
    s = settings.model_copy(update={"llm_role_label": "anthropic:claude-opus-5-5",
                                    "anthropic_api_key": SecretStr("test-key")})  # fmt: skip
    monkeypatch.setattr(script, "get_settings", lambda: s)
    labels.save(labels.from_announcements([stored("a1"), stored("a2")]),
                s.datasets_dir / "announcements_labels.parquet")  # fmt: skip
    monkeypatch.setattr(script, "build_router", lambda *a, **k: pytest.fail("made a paid call"))
    assert asyncio.run(script.label(None, confirm=False)) == 0
    out = capsys.readouterr().out
    assert "2 teacher calls on anthropic:claude-opus-5-5" in out and "dry run" in out


def test_calibrate_script_writes_the_map_and_report(settings, monkeypatch, capsys):
    script = load_script("calibrate_decision_models")
    monkeypatch.setattr(script, "get_settings", lambda: settings)
    monkeypatch.setattr(script, "laya_available", lambda: True)

    class Overconfident(FakeModel):
        def __init__(self, **kw: Any) -> None:
            self.calls = 0

        async def decide(self, state: Mapping[str, str], questions: Mapping[str, Question]):
            self.calls += 1
            answers = await super().decide(state, questions)
            if self.calls % 3 == 0:  # wrong a third of the time, 97% sure regardless
                answers["event_type"] = Answer("choice", "results", {"results": 0.97,
                                               "results_date": 0.03}, 0.97, model="laya:m")  # fmt: skip
            return answers

    monkeypatch.setattr(script, "LayaLocal", Overconfident)
    df = labels.from_announcements([stored(f"a{i}", f"text {i}") for i in range(60)])
    df = asyncio.run(labels.teacher_label(df, FakeRouter()))  # type: ignore[arg-type]
    labels.save(df, settings.datasets_dir / "announcements_labels.parquet")
    assert asyncio.run(script.run(["multilingual"])) == 0
    cmap = CalibrationMap.load(settings.models_dir / "calibration.json")
    t = cmap.temperatures[calibration.key("laya:multilingual", "announcements", "event_type")]
    assert t > 1.0
    assert "best: laya:multilingual" in capsys.readouterr().out
    assert list(settings.reports_dir.glob("calibration_*.json"))
