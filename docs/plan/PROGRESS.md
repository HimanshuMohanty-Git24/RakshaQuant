# Platform v2 progress

Source of truth: [2026-10-01-platform-v2-plan.md](2026-10-01-platform-v2-plan.md). Branch: `platform-v2`
(from `877c4ac`). Status values: `todo` / `doing` / `done` / `blocked`.

## M0: Foundation & hygiene

| Task | Status (todo/doing/done/blocked) | Commit | Notes / deviations |
|---|---|---|---|
| 0.1 | done | 6519073 | Branch `platform-v2` created; audit + plan committed. **Extra:** b4e8175 applies `ruff format` to 12 pre-existing unformatted files so the §8 format gate is green (AST-preserving; mypy count unchanged at 374). |
| 0.2 | done | (this commit) | Moved (not deleted) `paper_wallet.json` (sha256 91f86c5d…), `exit_manager_state.json` (44136fa3…), `dummy_journal.db` (4e5972e7…) to `var/archive/2026-10-01/`; `paper_idempotency.json` and `performance_history.json` were absent. Code default and `.env` both already give ₹10,00,000 (checked by match, value not printed); pinned by `tests/test_state_reset.py`. |
| 0.3 | todo | | |
| 0.4 | todo | | |
| 0.5 | todo | | |
| 0.6 | todo | | |
| 0.7 | todo | | |
| 0.8 | todo | | |

## M1: Domain types, event store, logging, calendar

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 1.1 | todo | | |
| 1.2 | todo | | |
| 1.3 | todo | | |
| 1.4 | todo | | |
| 1.5 | todo | | |
| 1.6 | todo | | |

## M2: Market data trust & session lifecycle

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 2.1 | todo | | |
| 2.2 | todo | | |
| 2.3 | todo | | |
| 2.4 | todo | | |
| 2.5 | todo | | |
| 2.6 | todo | | |

## M3: Execution core (OMS + SimulatedBroker + NSE costs)

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 3.1 | todo | | |
| 3.2 | todo | | |
| 3.3 | todo | | |
| 3.4 | todo | | |
| 3.5 | todo | | |
| 3.6 | todo | | |

## M4: Risk engine, kill switches, config bounds

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 4.1 | todo | | |
| 4.2 | todo | | |
| 4.3 | todo | | |
| 4.4 | todo | | |
| 4.5 | todo | | |

## M5: Strategies, TradePolicy, deterministic decision engine

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 5.1 | todo | | |
| 5.2 | todo | | |
| 5.3 | todo | | |
| 5.4 | todo | | |
| 5.5 | todo | | |
| 5.6 | todo | | |

## M6: LLM provider layer

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| registry | todo | | |
| clients (openai_compat, anthropic_native) | todo | | |
| router | todo | | |
| pricing | todo | | |
| prompts | todo | | |
| roles + `scripts/llm_check.py` | todo | | |

## M7: Decision models (Laya + Jev), announcements, event gate

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 7.1 | todo | | |
| 7.2 | todo | | |
| 7.3 | todo | | |
| 7.4 | todo | | Benchmark spike: do first in M7. |
| 7.5 | todo | | |
| 7.6 | todo | | |
| 7.7 | todo | | |
| 7.8 | todo | | |
| 7.9 | todo | | |

## M8: Paired books, shadow ledger, daily report, replay

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 8.1 | todo | | |
| 8.2 | todo | | |
| 8.3 | todo | | |
| 8.4 | todo | | |
| 8.5 | todo | | |
| 8.6 | todo | | |
| 8.7 | todo | | |

## M9: Web backend v2

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 9.1 | todo | | |
| 9.2 | todo | | |
| 9.3 | todo | | |
| 9.4 | todo | | |
| 9.5 | todo | | |
| 9.6 | todo | | |

## M10: Frontend v2

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 10.1 | todo | | |
| 10.2 | todo | | |
| 10.3 | todo | | |
| 10.4 | todo | | |
| 10.5 | todo | | |

## M11: Backtest parity & PIT data

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 11.1 | todo | | |
| 11.2 | todo | | |
| 11.3 | todo | | |

## M12: Cleanup, docs, scheduling, readiness & dry-run

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 12.1 | todo | | |
| 12.2 | todo | | |
| 12.3 | todo | | |
| 12.4 | todo | | |
| 12.5 | todo | | |

## Verify-flag outcomes

| Fact | Expected | Found | Action |
|---|---|---|---|

## Open questions for owner

- Your `.env` sets `LANGSMITH_TRACING_V2` to true, which opts in to sending traces to LangSmith (D12 makes it opt-in, off by default). I have not edited `.env`. Set it to `false` if you don't want traces leaving the machine.
