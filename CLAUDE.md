# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

RakshaQuant (Platform v2) is a **paper-trading** platform for NSE cash equities, built for a
controlled experiment. A deterministic engine trades three paired books on the same signals:
A has no advisor, B has a local typed decision model (Laya, escalating to Jev) as a veto, and C
has an LLM as a veto. The question is whether AI adds value net of its cost.

- Everything is recorded in an SQLite event store.
- There is **no broker order path**: every order fills on a simulated broker.
- Read [docs/architecture.md](docs/architecture.md) first.
- The build plan is [docs/plan/2026-10-01-platform-v2-plan.md](docs/plan/2026-10-01-platform-v2-plan.md),
  the source of truth. Its status, decisions log (OD-n) and open owner questions (OQ-n) are in
  [docs/plan/PROGRESS.md](docs/plan/PROGRESS.md).

Hard rules:

- **Never add a live-broker path or enable real orders.** Paper only, unless the owner
  explicitly asks.
- **Never print, log or echo values from `.env`**: it holds real credentials. Read settings in
  code via `get_settings()`; secrets are `SecretStr`. Run tools with
  `RAKSHAQUANT_ENV_FILE=none` when they don't need the real configuration.
- No pushing, PRs or paid API calls in loops without the owner's go-ahead.
- **During the month run**, the trading path is frozen except for P0 fixes:
  - `src/strategies`, `src/risk`, `src/oms`, `src/brokers/simulated` and `src/decision`;
  - `src/config/experiment.yaml` and the `RISK_*` limits.

  See [docs/runbooks/incident.md](docs/runbooks/incident.md).

## Commands

Dependencies are managed with [uv](https://github.com/astral-sh/uv). The import package is
`src` (`from src.<module> import ...`). Extras:

- `dev`: pytest, ruff, mypy, hypothesis, pytest-benchmark;
- `web`: FastAPI, uvicorn;
- `decision-local`: Laya, which pulls torch;
- `broker-dhan`: the DhanHQ SDK, unused by the engine.

```bash
uv sync --extra dev --extra web                     # add --extra decision-local for Laya
uv run python scripts/check_config.py               # readiness check (exit 2 on a bad config)
uv run python scripts/run_live_trading.py           # today's paper session, Rich terminal dashboard
uv run python scripts/run_live_trading.py --mode web            # same session, browser console
uv run python scripts/run_live_trading.py --demo [--mode web]   # bundled synthetic day, var/demo/
uv run python scripts/replay_day.py --date YYYY-MM-DD           # re-run a recorded day
uv run python scripts/daily_report.py --date YYYY-MM-DD         # regenerate a day's report
uv run python scripts/validate_strategy.py --start ... --end ...  # backtest + edge gate
```

Quality gates (plan §8), run before every commit:

```bash
uv run --extra dev ruff check . && uv run --extra dev ruff format --check .
# strict mypy on the v2 packages (the list in .github/workflows/ci.yml); must be clean:
uv run --extra dev --extra web mypy --follow-imports=silent src/domain src/store src/ops \
  src/marketdata src/reference src/brokers src/oms src/risk src/strategies src/features \
  src/decision src/decision_models src/llm src/evaluation src/engine src/web src/dashboard \
  src/backtesting src/config src/utils
uv run --extra dev --extra web python scripts/ci/mypy_ratchet.py <MYPY_MAX_ERRORS from ci.yml>
uv run --extra dev --extra web pytest -q           # hermetic; must leave the repo untouched
uv run --extra dev --extra web pytest tests/test_risk_engine.py::test_name   # one test
cd frontend && npm run gen:api && npm run typecheck && npm run lint && npm test \
  && npm run build && npm run check:bundle
cd frontend && PW_CHANNEL=msedge npm run e2e       # Playwright against the real demo server
```

`pytest` uses `asyncio_mode = "auto"`, and benchmarks are opt-in (`-m bench`). The mypy
ratchet is a ceiling on the global error count: lower `MYPY_MAX_ERRORS` in `ci.yml` when the
count drops, never raise it.

**Generated files**, which must be regenerated and committed (tests fail when stale):

- `frontend/openapi.json` and `frontend/src/api/types.gen.ts`: run
  `scripts/export_openapi.py`, then `npm run gen:api`, after any web model or route change.
- `docs/reference/*.md`: `scripts/gen_docs.py`, after changing events, settings, limits,
  reason codes, routes, packages or script docstrings.
- `src/engine/demo_tape/`: `scripts/build_demo_tape.py`.
- `tests/golden/*.jsonl`: `UPDATE_GOLDEN=1`, only when a change *should* alter recorded events.

## Architecture map

The live path is `src/engine/live.py` (`run_paper`, `run_demo`) → `src/engine/runner.py`
(`build_engine`; one `Book` per experiment book) → `src/engine/lifecycle.py` (PRE_OPEN → OPEN →
ENTRY_WINDOW 09:20-09:45 → MONITOR → CLOSE → REPORT → EXIT, from the NSE calendar).

| Package | Role |
| --- | --- |
| `domain` | frozen types, the event catalogue (`events.py`), ids, clocks, the NSE calendar |
| `store` | the event store (append + projections in one transaction), migrations, `kv_state`, the Parquet tape |
| `marketdata`, `reference` | YFinance quotes/history with validation; NSE reference data; announcements RSS; bhavcopy |
| `features`, `strategies` | features on settled bars; one module per strategy; `TradePolicy` |
| `decision` | `DecisionEngine` (signals → per-book proposals → advisors → `OMS.submit`); advisors |
| `risk` | `RiskEngine` + checks, `RiskGate`, daily risk state, kill switches, monitor, flattener |
| `oms`, `brokers` | OMS, position book, exit manager; `BrokerAdapter` protocol; the simulated broker and NSE costs |
| `llm`, `decision_models` | provider-agnostic LLM router; Laya/Jev cascade, calibration, announcement typing |
| `evaluation` | experiment config, books, shadow ledger, daily report, nightly review |
| `engine` | runner, lifecycle, tasks, market service, live/demo/replay drivers |
| `web`, `dashboard` | the FastAPI console (security, read model `queries.py`, controls, stream); the Rich CLI |
| `backtesting` | the paper engine over daily bars; edge statistics and the gate |
| `ops`, `config`, `notifications`, `utils` | logging/redaction/process contract; settings, limits, YAML configs; Telegram; IST helpers |

The full list is in [docs/reference/packages.md](docs/reference/packages.md).

## Invariants (do not regress)

### Orders and positions

- **Every order goes through `OMS.submit` → `RiskGate`.** That includes entries, exits,
  protective stops and kill-switch flattens. An OMS without a gate routes nothing.
  Exits are reduce-only intents and can never oversell or flip a position.
- `OrderSubmitted` is recorded **before** the broker call, and submission is shielded. A
  cancelled process never leaves an order the OMS doesn't know.
- Positions change **only** through fills (`PositionBook.apply`). P&L is net of both legs'
  charges, with one source.

### Risk

- Entries **fail closed**: a throwing check is `SYS_CHECK_ERROR`. Exits fail open, and only
  session, data and quantity rules can stop one.
- The gate builds a fresh snapshot per order and reserves working entries' capacity.
- Kill switches are persisted and **latching**: only an operator resume re-arms them. The
  HALT file is re-checked on every submit.
- Restarts never reset start-of-day equity, breaches or streaks.

### Data

- Simulated or synthetic prices can **never** create orders outside `ENVIRONMENT=demo`
  (`SYS_DATA_SIMULATED`).
- Features use **settled** daily bars only: dividend-adjusted for indicators, raw for
  prices and stops. A lagging series (Yahoo's NaN close) is skipped, never traded.
- **Time**: engine code uses the injected `Clock` and the calendar, never `datetime.now()`;
  IST via `src/utils/market_time.py`. The calendar fails closed outside its covered years
  (2026 only for now).

### Events

- Projections are pure functions of events: a rebuild must equal the incremental result
  (property-tested).
- Never mutate stored events. Payload changes need `schema_version` handling.
- Store schema changes need a numbered migration.
- Determinism is pinned by golden tests: serialise sets sorted, and keep ids deterministic.
  `intent_id` includes the book; `client_order_id` is a hash of it.

### AI

- Advisors **veto only**, and any failure is ABSTAIN, so the decision stands.
- `LLMRouter.complete` never raises.
- No positions or P&L ever go into a model's input. Prompts put data in JSON `<data>` blocks.
- An `evidence_ref` that names nothing in the input is a hallucination and is dropped.
- `TradeReview` lessons are never read by the trading path (a test enforces it).
- Paid LLM calls respect the INR budgets. Labelling is a dry run unless `--confirm-spend`.

### Web

- Every `/api/*` route needs the per-launch token, except `/api/health`.
- State-changing requests must pass the origin check; resume and flatten need the typed
  phrases.
- `src/web/queries.py` and `models.py` must stay FastAPI-free, because the CLI imports them
  (tested).

## Conventions and gotchas

### Code

- Python **3.11** syntax. New code must be ruff-clean and mypy-strict-clean (fully annotated).
- `# fmt: skip` keeps compact multi-line calls as written.
- Scripts insert the repo root into `sys.path`, use `run_entry_point(...)` for the exit codes
  (0/1/2/3), and get `--help` via argparse.
- Each script's docstring feeds `docs/reference/scripts.md`.
- Our HTTP code uses **`httpx2`**, not `httpx` (OD-1); tests mock with
  `httpx2.MockTransport`. Keep `numpy<2.5` (OD-2).
- Edit `pyproject.toml` by hand, then `uv lock`: `uv add` rewrites the whole file.

### Tests

- Tests are **hermetic**:
  - `tests/conftest.py` scrubs the environment and sets `RAKSHAQUANT_ENV_FILE=none`;
  - `var_dir` points under `tmp_path`;
  - NSE polling and Laya are off (`ANNOUNCEMENTS_ENABLED`, `DECISION_LAYA_ENABLED`).
- Use the `settings` fixture and `ReplayClock`.
- OMS mechanics tests use `tests/oms_harness.py` `unchecked` as the gate.
- CI installs only the `dev` and `web` extras. A local `.venv` with `decision-local` also has
  what Laya pulls in (e.g. `httpx` via huggingface-hub), which can hide a missing dependency.
  After changing dependencies or test imports, run the suite in a CI-like environment:
  `UV_PROJECT_ENVIRONMENT=<temp dir> uv run --locked --extra dev --extra web pytest`.

### Windows

- The host is Windows: use `pathlib`; open files can't be deleted.
- The repo mixes LF and CRLF: preserve each file's line endings when editing.
- The Playwright browser download times out on this machine, so run the E2E suite with
  `PW_CHANNEL=msedge`.

### Runtime

- Runtime state lives under `var/` (gitignored); never write elsewhere.
- One process per environment holds `var/<env>/` (exit 3 otherwise).
- The demo uses its own `var/demo/demo.db`, because the entry point keeps `db_path` open.

### Data sources

- Yahoo's daily `Close` is already split-adjusted (adjust dividends only).
- YFinance quotes are delayed.
- NSE's website terms restrict automated collection. The owner allows the announcements RSS
  feed and polite bhavcopy downloads (OD-10); don't add any other NSE scraping.

### Process

- One commit per plan task (conventional commits); update `docs/plan/PROGRESS.md` with the
  commit hash.
- Log delegated decisions as OD-n and owner questions as OQ-n.
