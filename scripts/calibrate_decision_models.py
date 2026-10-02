"""
Fit and report decision-model calibration (plan M7.5) on the labelled announcement set.

For each Laya checkpoint (and Jev when ``TYPESAFE_API_KEY`` is set) it predicts every labelled
announcement, fits a temperature per question on a deterministic 70% split, and scores the other
30% before and after: accuracy, Brier, ECE and reliability bins. It writes the temperatures to
``<var>/models/calibration.json`` (used by the cascade from then on) and the full report to
``<var>/reports/``, and names the checkpoint with the lowest mean calibrated Brier.

    uv run python scripts/build_decision_labels.py ...   # first: the labelled set
    uv run python scripts/calibrate_decision_models.py [--checkpoints multilingual english]
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.config import get_settings
from src.config.errors import ConfigError
from src.decision_models import calibration, labels
from src.decision_models.adapters import CONTEXT_TOKENS, LayaLocal, jev_remote
from src.decision_models.base import DecisionModel
from src.decision_models.setup import laya_available
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point

TASK = "announcements"


async def evaluate(model: DecisionModel, name: str, df: Any, context: int) -> dict[str, Any]:
    predictions = await labels.predict(model, df, context_tokens=context)
    results = {}
    for question, rows in predictions.items():
        heldout = labels.split([aid for aid, _, _ in rows])
        results[question] = calibration.calibrate(rows, question, heldout)
    return {"model": name, "questions": results}


def mean_brier(result: dict[str, Any]) -> float:
    reports = [q.after for q in result["questions"].values() if q.after.n]
    return sum(r.brier for r in reports) / len(reports) if reports else float("inf")


async def run(checkpoints: list[str]) -> int:
    settings = get_settings()
    df = labels.load(settings.datasets_dir / "announcements_labels.parquet")
    labelled = df[df["label_event_type"].notna()]
    if len(labelled) < 30:
        raise ConfigError(f"only {len(labelled)} labelled announcements: build the set first "
                          "(scripts/build_decision_labels.py)")  # fmt: skip
    models: list[tuple[DecisionModel, str, int]] = []
    if laya_available():
        for checkpoint in checkpoints:
            laya = LayaLocal(checkpoint=checkpoint, cache_dir=str(settings.models_dir / "hf"))
            models.append((laya, f"laya:{checkpoint}", CONTEXT_TOKENS[checkpoint]))
    key = settings.typesafe_api_key
    if key is not None and key.get_secret_value():
        models.append((jev_remote(key.get_secret_value(), model=settings.typesafe_model),
                       f"jev:{settings.typesafe_model}", 64_000))  # fmt: skip
    if not models:
        raise ConfigError(
            "no decision model: install the decision-local extra or set TYPESAFE_API_KEY"
        )

    temperatures: dict[str, float] = dict(calibration.CalibrationMap.load(
        settings.models_dir / "calibration.json").temperatures)  # fmt: skip
    results = []
    for model, name, context in models:
        print(f"evaluating {name} on {len(labelled)} announcements ...", flush=True)
        result = await evaluate(model, name, labelled, context)
        results.append(result)
        for question, q in result["questions"].items():
            temperatures[calibration.key(name, TASK, question)] = q.temperature
            print(f"  {question:12} T={q.temperature:<7} held-out n={q.after.n:<4} "
                  f"Brier {q.before.brier:.4f} -> {q.after.brier:.4f}  "
                  f"ECE {q.before.ece:.4f} -> {q.after.ece:.4f}  acc {q.after.accuracy:.3f}")  # fmt: skip
    best = min(results, key=mean_brier)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    calibration.CalibrationMap(temperatures).save(
        settings.models_dir / "calibration.json",
        meta={"fitted_at": stamp, "labelled": len(labelled), "best": best["model"]},
    )
    out = settings.reports_dir / f"calibration_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "fitted_at": stamp, "labelled": len(labelled), "best": best["model"],
        "models": [{"model": r["model"], "mean_calibrated_brier": round(mean_brier(r), 4),
                    "questions": {q: v.as_dict() for q, v in r["questions"].items()}}
                   for r in results],
    }, indent=2), encoding="utf-8")  # fmt: skip
    print(f"best: {best['model']} (mean calibrated Brier {mean_brier(best):.4f}); report {out}")
    return ExitCode.OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--checkpoints", nargs="+", default=["multilingual"],
                        choices=list(CONTEXT_TOKENS))  # fmt: skip
    return asyncio.run(run(parser.parse_args().checkpoints))


if __name__ == "__main__":
    run_entry_point("calibrate_decision_models", main, console_log_level="WARNING")
