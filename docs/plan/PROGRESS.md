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
| 1.2 | done | b91a582 | `src/domain/events.py`: frozen-dataclass `Event` envelope `{seq, ts_utc, ist_date, type, schema_version, decision_id, cycle_id, book_id, symbol, source, payload}` (a dataclass, not a Pydantic model, so subclass payload fields can't be dropped by base-typed serialisation). `make_event` stamps UTC + IST date, derives `decision_id`/`book_id`/`symbol` from the payload via `EventPayload.routing()` and **raises if an explicit value contradicts it**. 37 payload types: the full §G.2 catalogue plus `QuoteRejected` (M2.2), `TradeClosed` (trades projection), `TypedEvent` (M7.7), `DecisionModelCall` + `LLMCall` (M6/M7). Registry with `load_payload` refusing unknown types and unknown schema versions; canonical storage JSON (sorted keys, decimals as text). **Additions vs plan:** `book_id` in the envelope (M8/M9 need it); envelope `symbol` is the plain symbol (`INFY`). `tests/factories.py` has one sample per event type; tests assert the catalogue covers every payload class and that each round-trips. 46 tests. |
| 1.3 | done | 1cd11ea | `src/domain/ids.py`: `new_id()` = 16 hex digits of `time_ns` + 8 random hex, **strictly increasing within the process** (guards repeated clock readings on Windows; thread-safe). `intent_id(...)` and `client_order_id = sha256(intent_id)[:16]`. **Deviation:** `intent_id` includes `book_id` (`A:momentum:NSE:EQ:INFY:2026-10-02:entry`); without it books A/B/C would share intent and client-order ids for the same signal. Parts may not contain `:` (keeps the id unambiguous). 10 tests (5,000-id ordering, 8-thread uniqueness). |
| 1.4 | done | 7d90d12 | `src/domain/clock.py`: `Clock` protocol, `WallClock`, and `ReplayClock` (virtual-time scheduler: sleepers wake in time order, FIFO on ties, event loop settles between wakes; cancelled sleepers skipped). `src/domain/calendar.py` + `src/config/nse_calendar.json` (2026, every entry sourced): `is_trading_day`, `session(d) → {pre_open, open, close}`, `is_market_open(ts)` (half-open `[open, close)` IST), `next_session(ts)`, plus `next/previous_trading_day` and `trading_days` for M2.5/M7.8. Special sessions with un-notified times stay **closed**; uncovered years raise `CalendarCoverageError` (fail closed). `market_time.is_market_hours` now delegates to the calendar; the legacy risk hours rule requires a calendar session (holidays/weekends block; a coverage gap blocks instead of crashing). Four legacy tests that faked the clock with MagicMocks / 2024 dates now use real 2026 instants. 46 calendar+clock tests; also green under `TZ=America/New_York`. **Ops note:** the calendar covers 2026 only; add 2027 when NSE publishes it (Dec 2026) or the system will refuse to trade from 1 Jan 2027 (runbook item for M12). |
| 1.5 | done | 8c7a6ef | `src/store/`: `sqlite.connect` (WAL verified, `synchronous=NORMAL`, `busy_timeout=5000`, explicit `BEGIN IMMEDIATE` transactions) + numbered SQL migrations tracked in `PRAGMA user_version` (refuses newer schemas, rejects gaps). `EventStore`: `append`/`append_many` (event + projections in **one** transaction; a failing projector rolls the event back), `read(since_seq, types, decision_id, symbol, book_id, ist_date, limit)`, `last_seq`, `rebuild_projections`, read-only `query`, `snapshot_projections`. Ten projectors (orders, fills [dedup by `fill_id`], positions, trades, daily_risk_state, kill_switches, decisions, llm_calls, decision_model_calls, typed_events), each a pure function of the stored event. `TapeWriter`: Parquet per day per stream (quotes by IST receipt date, bars by recording day); crash-safe parts → compaction stamps `rq_max_part` so leftover parts are never read twice. Settings gain `var_dir` (state_dir = `var_dir/<env>`) and the §3 layout properties (`db_path`, `tape_dir`, `logs_dir`, …); tests isolate via `VAR_DIR`. **Acceptance:** Hypothesis property test (60 random event streams over small id pools, random batch sizes) shows rebuilt == incremental projections; **mutation-checked** (a wall-clock projector makes it fail). pyarrow added to the mypy ignore-missing-imports list (it ships no types). 24 tests. |
| 1.6 | done | c54d300 | `src/ops/context.py` (contextvars `cycle_id`, `decision_id`, `component`, `symbol`, `book_id`; `log_context()`; they follow work into tasks and `asyncio.to_thread`, tested). `src/ops/logging_config.py`: JSON-lines `var/logs/rakshaquant-YYYYMMDD.log` switching at **IST** midnight, optional console handler, noisy third-party loggers capped at WARNING, and `configure_logging()` returns a handle that restores the root logger exactly. Redaction covers the plan's patterns plus `Bearer`, `'access-token': ...` dict/JSON forms and `secret=`/`password=`; **widened `sk-` pattern** (the plan's `sk-[A-Za-z0-9]{8,}` misses `sk-ant-…`/`sk-or-v1-…`); applied per field *before* JSON encoding (a test caught that redacting the encoded line corrupted it) and to tracebacks. `src/ops/process.py`: `run_entry_point()` gives every script exit codes 0/1/2/3 (Ctrl-C = 0), redacted one-line stderr messages, config errors listed **without input values**, optional lock, and `ProcessStarted`/`ProcessStopped(reason, exit_code)` events. Wired into all 8 `scripts/*.py`: `run_live_trading` (lock + events; console logging off in CLI mode so the Rich dashboard is not overwritten; the legacy always-exit-0 handler is gone), `check_config` (now exits 2 when GROQ_API_KEY is missing; gained the `sys.path` line), `diagnose_risk`, `validate_strategy`, `setup`, `test_dhan_connection` (exit 1 on failure), and the legacy `run_trading` (its import-time `logs/` FileHandler removed) and `run_with_dashboard`. New settings: `log_level`; `RAKSHAQUANT_ENV_FILE` (path, or `none` to read no `.env`; the test harness exports `none` so test subprocesses are hermetic too). 37 tests incl. real child processes exiting 1/0/2. |

**M1 acceptance** (all verified 2026-10-02):
- Store round-trip: every event type appended and read back equal (`tests/test_store.py`).
- Rebuilt projections are identical to incremental ones: Hypothesis property test, mutation-checked.
- Calendar tests cover a weekday, a weekend, 2026-10-02 and a Muhurat session (`tests/test_domain_calendar.py`).
- Redaction tests pass for every secret shape, in messages, JSON fields and tracebacks.
- A crash yields exit code 1 in a real child process (`tests/test_ops_process.py`).

## M2: Market data trust & session lifecycle

| Task | Status | Commit | Notes / deviations |
|---|---|---|---|
| 2.1 | done | f425331 | `src/marketdata/yfinance_source.py`: one batched `yf.download(..., period="1d", interval="1m", auto_adjust=False, group_by="ticker", threads=True, progress=False)` per poll, in `asyncio.to_thread` under `wait_for` (20 s); parsing happens in the thread too. `exchange_ts` = last 1-minute bar (tz-aware IST), `receipt_ts` = clock now, `is_delayed=True`, `source=yfinance`, `volume_cum` = the day's summed 1-minute volume, `prev_close` from history. **Found:** a batched download *swallows* per-ticker errors incl. rate limits (logs them, returns NaN columns), so "no symbol returned data" is treated as a failed poll. Failed polls back off exponentially (60→120→240… capped at 900 s) and emit one feed-level `FeedStale` (then `FeedRecovered`); a timed-out download thread is never stacked. Per-symbol staleness (missing, or `exchange_ts` older than 1,200 s per audit F.0 while the market is open) emits separate `FeedStale` events for "no data" vs "too old". Valid quotes are taped each poll. New `EventSink` protocol (`src/domain/sink.py`, `RecordingSink`) and `StoreSink`. **Acceptance:** 15 symbols mocked ≤1 s ✓; **live: 2.61 s cold, ~0.8 s warm** (≤3 s) ✓; event-loop lag <50 ms during a 0.5 s blocking download over a 15×375-bar frame ✓ (probe test). |
| 2.2 | done | f425331 (with 2.1) | `src/marketdata/validation.py`: `QuoteValidator` rejects non-finite / non-positive prices, moves beyond `band + 2%` of the previous close, and cumulative volume that decreases within a session (resets on a new IST session); `RawQuote` carries vendor floats until validated. Rejected quotes are dropped with a `QuoteRejected` event (raw values, NaN as text) and never taped. Default band 20% (NIFTY 50 names are F&O stocks with no fixed band); per-instrument bands come from M2.4. **Deviation:** committed together with 2.1 because the validator is the poller's accept/reject gate. 21 tests for 2.1+2.2. |
| 2.3 | done | d125444 | `src/marketdata/history.py`: one batched `yf.download(period="1y", interval="1d", auto_adjust=False, actions=True)` in a thread under a timeout. `DailySeries.raw()` (prices/stops) and `.adjusted()` (indicators) from a **dividend-only** back-adjustment factor; `bars()` for the tape; settled bars only (`settled_before`); missing/short (<60 bars) series excluded and reported in `failed` (→ `Alert`), a failed download fails every symbol (no fallback, no synthetic data); OHLC ordering restored from reported values and counted. **Verified live:** all 50 NIFTY constituents + `^NSEI` in 4.3 s; adjusted closes match Yahoo's Adj Close to 1.5e-7. **Finding:** Yahoo returned the 1 Oct daily bar with a **NaN close** (open/volume present) late that night, so the series silently ended on 30 Sep; added `expected_last` → `lagging` + `history_lagging` alerts (the M2.5 pre-open passes `calendar.previous_trading_day(today)`). 11 tests. |
| 2.4 | done | c5eac5d | `src/reference/`: `download.fetch_snapshot` (dated, cached once per day as `var/reference/<name>_<date>.<ext>`, atomic, optional transform + validation; on failure falls back to the newest older snapshot flagged stale, else `ReferenceDataError`). `universe.py` (NIFTY 50 CSV, validated: 50 rows, unique symbols, ISIN shape, industry = sector map). `instruments.py` (Dhan scrip master filtered to NSE `E`/`EQUITY` rows before storing, **tick = `SEM_TICK_SIZE` / 100** (paise), lot, `broker_tokens={"dhan": security_id}`; unmapped symbols fall back to Rs 0.05 and are reported). `bands.py` (NSE `sec_list.csv`; `No Band` → None for dynamic-band F&O stocks; per-series default table otherwise). `refresh.refresh_reference(reference_dir, day, universe_day=…)` with the universe **pinned** to a snapshot date (D13) and every degradation reported for an `Alert`. Validator gets `band_lookup(instruments)`. **Live:** 6.8 s, 50/50 mapped, ticks {0.01×3, 0.05×17, 0.1×25, 0.5×2, 1×3}, all 50 `No Band`, 15 sectors, master stored as 0.9 MB instead of 25 MB. 15 tests (httpx2 `MockTransport`). |
| 2.5 | done | (this commit) | `src/engine/lifecycle.py`: a pure `Schedule` from the calendar (`state_at(ts)`, `transitions()`) and `SessionLifecycle.run()` walking HOLIDAY→exit 0, PRE_OPEN→OPEN 09:15→ENTRY_WINDOW 09:20→MONITOR 09:45→CLOSE 15:30→REPORT +15 min→EXIT +20 min with `clock.sleep_until`, emitting `SessionStateChanged` (+ `HolidaySkipped` with the holiday name / "weekend"). Guards ask the schedule for *now* (robust if the loop lags): `decisions_allowed()` only in ENTRY_WINDOW; `ai_call_allowed(purpose)` likewise except `ANNOUNCEMENTS` and `NIGHTLY_REVIEW`. Late start runs pre-open then jumps to the current state; a start after EXIT exits; a session where 09:20–09:45 doesn't fit (e.g. Muhurat) has no entry window; an uncovered calendar year raises `ConfigError` (exit 2). Hooks (`on_pre_open`, `on_state`) that fail raise a CRITICAL `Alert` and the day carries on. **Acceptance (frozen clock, `ReplayClock`):** 09:00 start waits ✓, holiday exits 0 ✓, no decisions after the cutoff ✓, Saturday blocked ✓. 17 tests. |
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
| yfinance batched 1m download (M2.1) | one call, honest timestamps | yfinance 1.7.0: `(ticker, field)` MultiIndex columns, index tz `Asia/Kolkata`; 1 Oct's last 1-minute bar was 15:14–15:15 IST (Yahoo lag). Unknown/failed tickers: NaN columns + a logged error, no exception. | Parser + "all empty = failed poll" rule. |
| NIFTY 50 constituents URL (M2.4) | `niftyindices.com/IndexConstituent/ind_nifty50list.csv` | 200 OK, 50 rows, columns `Company Name, Industry, Symbol, Series, ISIN Code`; 15 industries | Used as universe + sector map. |
| Dhan scrip master URL + fields (M2.4) | `images.dhan.co/api-data/api-scrip-master.csv`, tick/lot/security id | 200 OK, ~25 MB, ~200k rows; NSE equities = `SEM_EXM_EXCH_ID=NSE, SEM_SEGMENT=E, SEM_INSTRUMENT_NAME=EQUITY`; all 50 NIFTY names present (RELIANCE id 2885). **`SEM_TICK_SIZE` is in paise** (INFY 5.0 = Rs 0.05; USDINR 0.25 = Rs 0.0025). | Divide by 100; keep only NSE equity rows on disk. |
| NSE price-band file (M2.4, optional) | an NSE band file | `nsearchives.nseindia.com/content/equities/sec_list.csv`: `Symbol, Series, Security Name, Band, Remarks`; Band ∈ {2,5,10,20,40,No Band}; every NIFTY 50 name is `No Band` (F&O, dynamic bands). | Wired as the band source; `No Band` → None. |
| Yahoo daily prices: split/dividend adjustment (M2.3) | "raw" vs adjusted | With `auto_adjust=False`, Yahoo's Close is **already split-adjusted** (no jump across 10 NIFTY 50 splits 2024–26, e.g. RELIANCE 2:1 Oct 2024) but not dividend-adjusted; Adj Close = Close × Π(1 − D/C_prev). | Adjust dividends only; never re-apply splits. |
| Yahoo latest daily bar completeness (M2.3) | yesterday's close available pre-open | 1 Oct bar had NaN Close (Open, Volume present) hours after the close; 1-minute data also stopped at 15:15 IST that day. | Lagging detection + alert. **Follow-up:** source the last settled close from NSE's official bhavcopy (M11.3 fetcher) and have M5 skip lagging symbols; re-check Yahoo's behaviour at the next pre-open. |
| NSE 2026 holidays incl. 2 Oct (M1.4) | 2 Oct 2026 is a holiday | Confirmed from NSE's own `/api/holiday-master?type=trading` (CM segment): 20 entries incl. **02-Oct-2026 Mahatma Gandhi Jayanti** and an ad-hoc **15-Jan-2026** (Maharashtra municipal election); matches circular NSE/CMTR/71775 (12 Dec 2025). | Encoded in `src/config/nse_calendar.json` with sources. |
| NSE session timings (M1.4) | pre-open 09:00, open 09:15, close 15:30 | NSE market-timings page: pre-open order entry 09:00–09:08, normal market 09:15–15:30, closing session 15:40–16:00 | Default session 09:00/09:15/15:30. |
| Special sessions 2026 (M1.4) | Muhurat on Diwali | Sunday **1 Feb 2026** live Budget session at standard timings (circular NSE/CMTR/72349). Muhurat **date** 8 Nov 2026 (Sunday) set by NSE/CMTR/71775; **timings not yet notified** (none on NSE's circulars page as of 18 Sep 2026). | Budget session encoded; Muhurat encoded with null times = closed. **Action:** fill in the Muhurat times when NSE's October circular lands. |
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
