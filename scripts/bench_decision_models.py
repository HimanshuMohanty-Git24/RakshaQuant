"""
Decision-model CPU benchmark (plan M7.4): load time, resident memory and p50/p95 latency of each
Laya checkpoint on this machine, for one question and for five questions per state.

The model weights are cached under ``<var>/models/hf`` (``HF_HOME``), not the user profile, so a
small system drive is not filled. Results are written as JSON to ``<var>/reports/``.

    uv sync --extra dev --extra web --extra decision-local
    uv run python scripts/bench_decision_models.py [--checkpoints english multilingual typed-decisions]
"""

import argparse
import ctypes
import gc
import json
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.config import get_settings
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point

SUBFOLDERS = {"english": None, "multilingual": "multilingual", "typed-decisions": "typed-decisions"}

STATE = (
    "Company: Infosys Limited (NSE: INFY), sector Information Technology.\n"
    "Announcement title: Outcome of Board Meeting - Unaudited financial results for the quarter "
    "and half year ended September 30, 2026.\n"
    "Text: The Board of Directors at its meeting held today approved the unaudited standalone and "
    "consolidated financial results for the quarter ended September 30, 2026. Revenue grew 4.1% "
    "quarter on quarter in constant currency; operating margin was 21.3%, down 40 basis points. "
    "The Board declared an interim dividend of Rs 21 per share and revised the full-year revenue "
    "growth guidance to 3-4% from 2-3%. Large deal wins were USD 2.4 billion in the quarter."
)

QUESTIONS: dict[str, dict[str, Any]] = {
    "relevant": {"type": "noul", "instructions": "Is this announcement relevant to the share price?",
                 "criteria": {"false": "routine or immaterial", "true": "could move the price"}},
    "event_type": {"type": "choice", "instructions": "What kind of corporate event is this?",
                   "criteria": {k: k.replace("_", " ") for k in (
                       "results", "results_date", "dividend", "split_bonus", "pledge",
                       "insider_or_promoter", "order_win", "litigation_or_regulatory",
                       "management_change", "rating_change", "fundraise", "other")}},
    "direction": {"type": "choice", "instructions": "Likely price direction for shareholders?",
                  "criteria": {"positive": "good news", "negative": "bad news",
                               "neutral": "no effect", "unclear": "cannot tell"}},
    "materiality": {"type": "score", "instructions": "How material is this event?",
                    "criteria": ["minor", "moderate", "major"]},
    "veto": {"type": "noul", "instructions": "Is there a concrete adverse event that makes opening "
             "a long position today imprudent?",
             "criteria": {"false": "no adverse event", "true": "adverse event present"}},
}  # fmt: skip


def rss_mb() -> tuple[float, float]:
    """(current, peak) working set of this process in MB (Windows; 0 elsewhere)."""
    if platform.system() != "Windows":
        return 0.0, 0.0

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]  # fmt: skip

    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    psapi = ctypes.WinDLL("psapi")  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # a 64-bit pseudo-handle
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters),
                                           ctypes.c_ulong]  # fmt: skip
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                                      counters.cb):  # fmt: skip
        return 0.0, 0.0
    return counters.WorkingSetSize / 2**20, counters.PeakWorkingSetSize / 2**20


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def timed(agent: Any, questions: dict[str, Any], runs: int) -> dict[str, float]:
    for _ in range(3):
        agent.predict(STATE, questions)  # warm-up
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        agent.predict(STATE, questions)
        samples.append((time.perf_counter() - started) * 1000)
    return {"p50_ms": round(statistics.median(samples), 1),
            "p95_ms": round(percentile(samples, 0.95), 1),
            "min_ms": round(min(samples), 1), "runs": runs}  # fmt: skip


def bench(name: str, runs: int) -> dict[str, Any]:
    import laya
    import torch

    subfolder = SUBFOLDERS[name]
    gc.collect()
    before, _ = rss_mb()
    started = time.perf_counter()
    agent = laya.load("convaiinnovations/laya", subfolder=subfolder, device="cpu")
    first_load_s = time.perf_counter() - started  # includes the download on a cold cache
    loaded, _ = rss_mb()  # the first load's resident cost (torch keeps it after `del`)
    del agent
    gc.collect()
    started = time.perf_counter()
    agent = laya.load("convaiinnovations/laya", subfolder=subfolder, device="cpu")
    load_s = time.perf_counter() - started
    one = timed(agent, {"relevant": QUESTIONS["relevant"]}, runs)
    five = timed(agent, QUESTIONS, runs)
    answers = agent.predict(STATE, QUESTIONS)["answers"]
    current, peak = rss_mb()
    del agent
    gc.collect()
    return {
        "checkpoint": name,
        "first_load_s": round(first_load_s, 1),
        "load_s": round(load_s, 1),
        "rss_model_mb": round(loaded - before, 0),
        "rss_mb": round(current, 0),
        "peak_rss_mb": round(peak, 0),
        "torch_threads": torch.get_num_threads(),
        "one_question": one,
        "five_questions": five,
        "sample_answers": {
            k: {kk: vv for kk, vv in v.items() if kk != "probabilities"} for k, v in answers.items()
        },  # fmt: skip
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", nargs="+", default=list(SUBFOLDERS),
                        choices=list(SUBFOLDERS))  # fmt: skip
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()
    settings = get_settings()
    os.environ.setdefault("HF_HOME", str(settings.models_dir / "hf"))
    import torch

    results = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": {
            "cpu": platform.processor(),
            "system": platform.platform(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "torch": torch.__version__,
        },  # fmt: skip
        "runtime": "torch (cpu)",
        "checkpoints": [],
    }
    for name in args.checkpoints:
        print(f"benchmarking {name} ...", flush=True)
        result = bench(name, args.runs)
        results["checkpoints"].append(result)
        five = result["five_questions"]
        print(f"  load {result['load_s']} s, +{result['rss_model_mb']} MB, 1q p50 "
              f"{result['one_question']['p50_ms']} ms, 5q p50 {five['p50_ms']} ms / p95 "
              f"{five['p95_ms']} ms", flush=True)  # fmt: skip
    out = settings.reports_dir / f"decision_bench_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"written {out}")
    return ExitCode.OK


if __name__ == "__main__":
    run_entry_point("bench_decision_models", main, console_log_level="WARNING")
