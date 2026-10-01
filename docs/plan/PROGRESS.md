# Platform v2 progress

Source of truth: [2026-10-01-platform-v2-plan.md](2026-10-01-platform-v2-plan.md). Branch: `platform-v2`
(from `877c4ac`). Status values: `todo` / `doing` / `done` / `blocked`.

## M0: Foundation & hygiene

| Task | Status (todo/doing/done/blocked) | Commit | Notes / deviations |
|---|---|---|---|
| 0.1 | done | 6519073 | Branch `platform-v2` created; audit + plan committed. **Extra:** b4e8175 applies `ruff format` to 12 pre-existing unformatted files so the §8 format gate is green (AST-preserving; mypy count unchanged at 374). |
| 0.2 | done | 6b7df23 | Moved (not deleted) `paper_wallet.json` (sha256 91f86c5d…), `exit_manager_state.json` (44136fa3…), `dummy_journal.db` (4e5972e7…) to `var/archive/2026-10-01/`; `paper_idempotency.json` and `performance_history.json` were absent. Code default and `.env` both already give ₹10,00,000 (checked by match, value not printed); pinned by `tests/test_state_reset.py`. |
| 0.3 | done | 4b094da | `tests/conftest.py`: session-wide `pytest_configure` guard (env_file disabled, secrets + every Settings-field env var scrubbed, placeholders for required keys) so collection-time imports are hermetic too; autouse fixture adds `chdir(tmp_path)`, cache clears, and a full `os.environ` restore per test. `settings` fixture. Fixed the 3 named tests (explicit tmp engine / tmp journal / explicit no-creds settings). Proof: `tests/test_hermetic.py`; full suite **383 passed**; repo-root file sha256 and `git status` identical before/after the run. |
| 0.4 | done | b891288 | `environment` (dev/paper/demo/test) + absolute `state_dir` (default `<repo>/var/<env>`; relative values resolve against the repo root, never CWD). `.env` resolved from `REPO_ROOT` (proven by loading from a foreign CWD). LangSmith key optional, tracing default off; `setup_tracing` now validates the key **before** exporting env vars, and is a no-op unless opted in. `telegram_bot_token` / `database_url` are `SecretStr` (consumers updated). **Decision:** code default `environment=dev`, so ad-hoc runs never touch experiment state; the month run must set `ENVIRONMENT=paper` (goes into `.env.example` in M12). **Extra (rule 7, RQ-01):** the four legacy CWD-relative state files (wallet, idempotency, exit-manager, performance history) now live under `state_dir`, and their writers create the directory. Tests get `STATE_DIR` under `tmp_path` and an in-memory `DATABASE_URL`. Suite 399 passed; mypy 374 → 373. |
| 0.5 | done | 9c31f75 | New `src/ops/` package: `instance_lock.single_instance(state_dir)` (OS lock via `filelock` 4.0.8, non-blocking, released on crash) and `exit_codes.ExitCode` (0/1/2/3; M1.6 wires the rest). `scripts/run_live_trading.py` takes the lock **before** the legacy `finally: sys.exit(0)` so exit 3 can't be masked; different environments don't block each other. `filelock` added now (rest of §3 deps in 0.7); `pyproject.toml` hand-edited because `uv add` rewrote the whole file. Proof: `tests/test_instance_lock.py` (in-process, cross-process child, real entry point with the loop stubbed) and a real shell run against `var/dev` printed the message and exited **3**. **Gate note:** strict mypy on new packages must use `--follow-imports=silent`, otherwise it reports legacy modules they import (`src/web` → 348). Suite 404 passed. |
| 0.6 | done | 5950849 | Root `.gitignore` rewritten with explicit entries (`var/`, `.env`, `*.db`, `.coverage`, `logs/`, `frontend/node_modules/`, `frontend/dist/`, `*.tsbuildinfo`, …); removed `*.json`, `lib/`, `data/` and also `*.csv` (equally blunt; all generated CSVs go to `var/`). Root-only packaging dirs anchored (`/build/`, `/dist/`). Added explicit `.tmp-ui-shots/` (was only hidden by `*.json`) and `.claude/settings.local.json`. Removed the now-dead `!package.json` / `!src/lib/` workaround from `frontend/.gitignore`. `git check-ignore` on `frontend/package.json` and `frontend/src/lib/*.ts`: not ignored (exit 1). The ignored/untracked set is identical before/after apart from the two edited files. Guarded by `tests/test_repo_hygiene.py`. Suite 406 passed. |
| 0.7 | done | 2449eb6 | Added runtime `openai` 3.22, `anthropic` 1.11, `httpx2` 2.13, `pyarrow` 25, `pyyaml` 6.0.3 (`filelock` in 0.5); dev `pytest-benchmark`, `hypothesis`; `web` pins `fastapi>=0.140`; new `decision-local` extra `laya[onnx]>=0.3.3` (resolves 0.3.23 + torch 2.14; not installed by default). `uv lock --upgrade`: 105 → 167 locked packages, incl. pandas 2.3→**3.0**, SQLAlchemy 2.0→2.1, mypy 1.19→**2.3**, ruff 0.14→0.16, langgraph 1.0→1.2, yfinance 1.0→1.7, aiohttp 3.14.3, cryptography 50. All 406 tests pass unchanged. **Deviations:** (1) `httpx2` instead of `httpx`, and no `respx`: both official SDKs now depend on httpx2 (Starlette's TestClient also deprecates httpx), httpx's last release is Dec 2024, respx only supports httpx, and `httpx2.MockTransport` covers mocking. (2) `numpy<2.5`: 2.5 needs Python ≥3.12 and its stubs use 3.12 syntax, which broke mypy on the 3.12 dev venv vs the 3.11 target. (3) ruff 0.16 now formats code blocks in Markdown: added `extend-exclude = ["*.md"]` (docs and the dated audit are not code). (4) ruff UP042: `ExecutionMode` → `StrEnum` (all call sites use `.value`). **mypy ratchet:** 373 → 374 = the plan's start value; the +1 is requests 2.34's new type hints flagging a pre-existing `None` header in `src/market/live_data.py` (dead code, deleted in M12); the rest of the diff is mypy 2.x rewording. **Audits:** pip-audit over all extras: 141 packages audited, 0 skipped, **0 vulnerabilities** (audit: 67). Frontend: vite 5→**8.3.2**, `@vitejs/plugin-react` 4→6.1.1; `npm audit`: 3 (2 high, 1 moderate) → **0**; `--omit=dev`: 0 → 0. Build 16 s → 1.2 s, same 56 KB gz JS; rendered and streaming in headless Edge against `--demo`. `frontend/.npmrc` pins the https registry project-locally (global config untouched; it is `http://` on this machine); docs updated (Node `^20.19`/`>=22.12`). |
| 0.8 | done | 72e8722 | `.github/workflows/ci.yml`: ruff (check + format); pytest on 3.11 on **ubuntu and windows** (extra: the paper run is hosted on Windows); pytest with `TZ=America/New_York`; mypy strict on whichever §8 v2 packages exist (`--follow-imports=silent`; today `src/ops`, `src/risk`, `src/web`) plus the global ratchet via `scripts/ci/mypy_ratchet.py` (ceiling 374; fails if mypy didn't finish, since a blocking error reads as "1 error"; unit-tested); pip-audit 2.10.1 over every locked extra; frontend `npm ci && npm run build && npm audit --omit=dev`; `git status --porcelain` must be empty after tests/build. All `uv run` calls use `--locked`. Windows job pins `core.autocrlf=false` (repo mixes LF/CRLF). `.github/dependabot.yml`: weekly grouped updates for uv, npm and (extra) github-actions. **Found while verifying:** `astral-sh/setup-uv` publishes no floating `v10` tag (only `v10.2.0`), so `@v10` would have broken every job; all actions are now **pinned by commit SHA** with version comments. Validation: actionlint 1.7.12 + shellcheck 0.11 (canary proved shellcheck live): 0 findings; both files pass GitHub's JSON schemas. Not run on GitHub (no push without your OK); every job's commands were run locally verbatim and pass. Generated-type drift check arrives with M9.5. Suite 413 passed. |

**M0 acceptance** (all verified 2026-10-02):
- Full suite passes on this machine with the real `.env` present: 413 passed (the harness never reads it).
- Repo-root file sha256s, `git status --porcelain --ignored` and `var/` are identical before and after `pytest`.
- A second instance refuses to start: real shell run against `var/dev` printed the message and exited 3.
- The CI workflow is valid: actionlint + shellcheck clean, schema-valid; action refs verified to exist.

## M1: Domain types, event store, logging, calendar

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 1.1 | done | a913430 | `src/domain/base.py` (frozen, extra-forbidding `DomainModel`; `EventPayload` with `event_type`/`schema_version` class vars; constrained `Price`/`Money` Decimal and finite-float aliases) and `src/domain/types.py`: enums (Side, OrderType, Product, Validity, OrderStatus, IntentKind, IntentReason, IntentSource, Timeframe, MarketDataSource, SessionState, KillSwitchState/Scope, CheckLevel/Outcome, RiskOutcome, ReasonCode with derived `.level`, AdvisorKind, Verdict, announcement enums) and models Instrument, Quote, Bar, SignalReason, Signal, OrderIntent, Order, Fill, Position, RiskCheckResult, RiskSnapshotSummary, KillState, RiskDecision, VerdictReason, AdvisorVerdict, TypedEvent. Invariants enforced in the types (OHLC consistency, reduce_only ⇔ reducing kind, order-type prices, fill bounds, avg_price ⇔ open, check level ⇔ code, RESIZE ⇔ max_qty, outcome ⇔ qty, ABSTAIN ⇔ reason). **Design notes:** `OrderIntent.quantity` is None until risk sizes it (M4.1 RESIZE); `RiskDecision`, `AdvisorVerdict` and `TypedEvent` *are* event payloads; `book_id` on every OMS record (M8); `EVT_*` codes report at order level. 25 tests. |
| 1.2 | done | (this commit) | `src/domain/events.py`: frozen-dataclass `Event` envelope `{seq, ts_utc, ist_date, type, schema_version, decision_id, cycle_id, book_id, symbol, source, payload}` (a dataclass, not a Pydantic model, so subclass payload fields can't be dropped by base-typed serialisation). `make_event` stamps UTC + IST date, derives `decision_id`/`book_id`/`symbol` from the payload via `EventPayload.routing()` and **raises if an explicit value contradicts it**. 37 payload types: the full §G.2 catalogue plus `QuoteRejected` (M2.2), `TradeClosed` (trades projection), `TypedEvent` (M7.7), `DecisionModelCall` + `LLMCall` (M6/M7). Registry with `load_payload` refusing unknown types and unknown schema versions; canonical storage JSON (sorted keys, decimals as text). **Additions vs plan:** `book_id` in the envelope (M8/M9 need it); envelope `symbol` is the plain symbol (`INFY`). `tests/factories.py` has one sample per event type; tests assert the catalogue covers every payload class and that each round-trips. 46 tests. |
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
| `laya` package identity and extra names (M0.7) | pip `laya`, Convai Innovations, Apache-2.0, extra `onnx` | PyPI `laya` 0.3.23: author Convai Innovations, Apache-2.0, homepage huggingface.co/convaiinnovations/laya; extras `serve, fast, mcp, structured, onnx, langchain, langgraph, llamaindex, crewai`. Base install requires torch ≥2.0. | Used `laya[onnx]>=0.3.3` in the `decision-local` extra. `serve` exists (relevant to M7 LayaHTTP). |
| anthropic SDK ≥1.x HTTP stack (§3) | uses `httpx2` | anthropic 1.11.0 and openai 3.22.1 both require `httpx2` | Standardised our own HTTP code on `httpx2` (see 0.7). |

## Decisions log

Decisions the owner delegated on 2026-10-02 ("take the best possible decision and go forward"):

| # | Decision | Rationale |
|---|---|---|
| OD-1 | Our own HTTP code uses `httpx2`; no `httpx`/`respx` | Both official SDKs depend on httpx2; one HTTP stack; `httpx2.MockTransport` for tests. |
| OD-2 | `numpy<2.5` until the target moves to Python 3.12 | numpy 2.5 requires 3.12 and its stubs break mypy for the 3.11 target. |
| OD-3 | Code default `ENVIRONMENT=dev`; the month run sets `ENVIRONMENT=paper` | Ad-hoc runs can never touch experiment state. |
| OD-4 | LangSmith tracing **off** in the owner's `.env` (`LANGSMITH_TRACING_V2=false`, set 2026-10-02; only that line changed, no values printed) | D12 + audit F-27: traces would send capital, positions and P&L off-machine. |
| OD-5 | mypy ratchet ceiling 374 (= the plan's start value); Windows pytest job kept in CI | The +1 is upstream-detected in dead code deleted in M12; the paper run is hosted on Windows. |

## Open questions for owner

- None.
