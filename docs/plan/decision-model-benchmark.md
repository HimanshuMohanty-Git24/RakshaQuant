# Decision-model CPU benchmark (plan M7.4)

Measured 2026-10-02 on the owner's laptop. Script: `scripts/bench_decision_models.py`
(raw JSON in `var/reports/decision_bench_*.json`).

**Machine:** Intel Core i5-1240P (12 cores, 16 threads), 15.7 GB RAM, Windows 11, Python 3.12,
torch 2.14.1+cpu (12 intra-op threads), laya 0.3.23 (pinned checkpoint revisions), onnxruntime 1.30.

**Workload:** one announcement-sized state (≈ 250 tokens) and the M7.7 question set: `relevant`
(noul), `event_type` (choice, 12 labels), `direction` (choice, 4), `materiality` (score, 3), plus
the M7.9 `veto` (noul). "1 question" = `relevant` alone. 3 warm-up calls, then 30 timed calls,
nothing else running.

## Results

| Checkpoint | Runtime | 1 question p50 / p95 | 5 questions p50 / p95 | Model RAM / process peak | Load (cached) |
|---|---|---|---|---|---|
| `laya` (english, 421M) | torch CPU | 540 / 862 ms | 4,236 / 4,378 ms | 1.85 GB / 3.1 GB | 0.7 s |
| `laya-multilingual` (322M) | torch CPU | **208 / 220 ms** | **1,589 / 1,650 ms** | 1.52 GB / 2.4 GB | 0.6 s |
| `laya-typed-decisions` (421M) | torch CPU | 531 / 586 ms | 4,122 / 4,213 ms | 1.85 GB / 3.1 GB | 0.8 s |
| `laya-multilingual` | ONNX fp32 | 162 / 183 ms | 1,490 / 1,570 ms | — | — |
| `laya-multilingual` | ONNX int8 (dynamic) | 116 / 132 ms | 897 / 962 ms | 318 MB file | — |

First (cold) download: ~1.3–1.7 GB per checkpoint, cached in `var/models/hf` (not the system drive).

### Notes

- **Target missed.** The plan's target is p95 ≤ 300 ms per state with ≤ 5 questions. The best
  faithful configuration (multilingual, ONNX fp32) is ~1.6 s — about 5× over.
- **Cost driver:** all questions of a state run in one batched forward pass (one row per
  question), so cost grows with rows × tokens; the 12-label `event_type` choice dominates.
- **ONNX:** laya ships an `ONNXAgent` runtime but no export and no published `.onnx` files. We
  exported the multilingual `DecisionModel` ourselves (`torch.onnx.export`, opset 18; inputs
  `input_ids, attention_mask, marker_pos, marker_mask, qtype` → `logits, act_logits`).
  - fp32 ONNX: answers **identical** to torch (max probability difference 0.0), only ~7% faster —
    not worth an extra artefact.
  - **int8 dynamic quantisation is unusable as is:** ~45% faster but it changes the answers
    (`event_type` results → dividend, `direction` positive → neutral, probabilities off by up to
    0.59). It would need calibration-aware (static/QAT) quantisation and re-validation.
- **Calibration warning from laya:** the checkpoints ship invalid temperatures for choice
  questions with 11+ labels (clamped to 0.5; "treat confidence from the affected entries as
  uncalibrated"). Our own calibration (7.5) must cover them; the cascade already sends > 20-label
  choices to Jev.
- **Zero-shot quality is weak, as the plan expected:** on the sample earnings announcement torch
  multilingual put P(relevant) at 0.08. Calibration and probably fine-tuning on our labels (7.5)
  are required before trusting any answer.

## Decision

- **Default checkpoint: `laya-multilingual`, torch CPU** — the fastest by 2.6× at 0.8 GB less
  RAM, a 1,024-token context (twice the English checkpoint's), and identical answers to its ONNX
  export. To be confirmed by the calibration report (7.5).
- **The 300 ms target cannot be met on this laptop's CPU.** Per the plan the remaining options
  are LayaHTTP on another (GPU) machine or Jev-only; both need the owner (hardware, or a TypeSafe
  early-access key). Recommended instead: accept an operational budget of **p95 ≤ 2 s per state
  (≤ 5 questions)** on CPU. Decisions are daily-bar swing entries made once per session and
  announcements polled every 5 minutes, so ~1.6 s per state is well inside the entry window and
  runs on its own executor without blocking the event loop. Logged as an open question in
  `PROGRESS.md`.
