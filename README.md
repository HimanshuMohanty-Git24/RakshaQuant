# RakshaQuant

**A paper-trading platform for NSE equities, built to measure whether AI improves a trading
system, net of its cost.**

*Raksha* (रक्षा) means protection: the system is built around its risk controls.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Paper trading only. Educational and research use. Not investment advice.** The engine has
> no path to a real broker: every order fills on a simulated broker.

## What it does

A deterministic engine trades NSE cash equities (NIFTY 50, CNC, long only). It does this for
three **paired books** that see exactly the same signals:

| Book | Entries |
| --- | --- |
| **A** | every signal the strategies and risk rules allow |
| **B** | the same, but a local **typed decision model** (Laya) may veto an entry |
| **C** | the same, but an **LLM** may veto an entry |

AI can only veto. It never sizes, prices or adds an order, and an AI failure simply leaves the
deterministic decision in place. Every decision is recorded as events, with its full lineage:

- the signal and its features;
- each book's advisor verdict and evidence;
- the risk checks, the order, the fills and the exit;
- a counterfactual trade.

So the difference between the books, net of AI spend, is measurable. A pre-registered
month-long experiment ([pre-registration](docs/experiments/2026-10-month1-preregistration.md))
decides whether each advisor stays.

Highlights:

- **Risk first.** Every order, exits included, goes through one OMS gate that re-checks
  bounded limits on a fresh snapshot. Kill switches are persisted and latching, and a `HALT`
  file stops everything.
- **Realistic fills.** Latency, spread, impact, liquidity caps and NSE charges.
- **Data you can trust.** Quotes are validated and the calendar fails closed. Simulated prices
  can never trade outside the demo.
- **Any AI provider**, switched by configuration: OpenAI, OpenRouter, Groq, Anthropic, Ollama
  or any OpenAI-compatible endpoint. Each comes with fallbacks, circuit breakers and INR
  budgets.
- **An event store** (SQLite) as the system of record. The daily report, replays and both
  consoles are built from it.
- **Two consoles**: a terminal dashboard, and a local, token-protected web terminal.
- **Backtests that run the paper engine itself**, with a statistical go/no-go gate.

## Quick start

You need Python 3.11+ and [uv](https://github.com/astral-sh/uv). The web console also needs
Node `^20.19` or `>=22.12`.

```bash
uv sync --extra web                                # install
uv run python scripts/run_live_trading.py --demo   # a full demo session in the terminal, no keys needed
```

The demo replays a bundled synthetic day through the real engine in its own `demo`
environment. Book B's veto is scripted, so you can watch a veto avoid a loss.

For the browser console:

```bash
(cd frontend && npm ci && npm run build)
uv run python scripts/run_live_trading.py --mode web --demo   # prints a one-time console URL
```

To run a real paper session on today's market:

```bash
cp .env.example .env            # then set ENVIRONMENT=paper; every key is optional
uv run python scripts/setup.py  # creates .env if needed and runs the readiness check
uv run python scripts/run_live_trading.py --mode web   # or without --mode for the terminal
```

On a holiday or weekend the session exits at once. Started before the open, it waits for
pre-open (09:00 IST); started after the day's session, it exits.

## Documentation

| | |
| --- | --- |
| [Architecture](docs/architecture.md) | How the parts fit: the flow, a trading day, books, risk, AI, the event store |
| [Runbooks](docs/runbooks/daily-ops.md) | [Daily operations](docs/runbooks/daily-ops.md), [kill switches](docs/runbooks/kill-switch.md), [incidents](docs/runbooks/incident.md), [AI providers](docs/runbooks/providers.md) |
| Reference (generated from the code) | [Settings](docs/reference/settings.md), [risk limits and reason codes](docs/reference/risk.md), [events](docs/reference/events.md), [HTTP API and stream](docs/reference/api.md), [packages](docs/reference/packages.md), [scripts](docs/reference/scripts.md) |
| [Plan](docs/plan/2026-10-01-platform-v2-plan.md) and [progress](docs/plan/PROGRESS.md) | The Platform v2 build: every decision and verified fact |
| [Audit](docs/audit/2026-10-01-platform-audit.md) | The review of the earlier system that motivated v2 |

## Development

```bash
uv sync --extra dev --extra web                  # add --extra decision-local for Laya (pulls torch)
uv run --extra dev pytest                        # the test suite (hermetic: never reads your .env)
uv run --extra dev ruff check . && uv run --extra dev ruff format --check .
uv run --extra dev mypy src                      # strict; CI also runs a global error ratchet
cd frontend && npm run typecheck && npm run lint && npm test && npm run build
```

Some files are generated from the code, and tests fail when they go stale:

| File | Regenerate with |
| --- | --- |
| `frontend/openapi.json`, `frontend/src/api/types.gen.ts` | `uv run --extra web python scripts/export_openapi.py`, then `cd frontend && npm run gen:api` |
| `docs/reference/` | `uv run --extra web python scripts/gen_docs.py` |
| `src/engine/demo_tape/` | `uv run python scripts/build_demo_tape.py` |
| `tests/golden/` | run the golden tests with `UPDATE_GOLDEN=1` |

Validate a strategy before trusting it:
`uv run python scripts/validate_strategy.py --start 2025-01-01 --end 2025-12-31`. It reports
VALIDATED only with at least 200 trades and a 95% confidence interval of the mean net return
per trade above zero.

## Licence and disclaimer

MIT ([LICENSE](LICENSE)). RakshaQuant is a research project. It is **not** financial advice,
it trades on paper only, and its results say nothing about live trading. Algorithmic trading
involves significant risk of loss.
