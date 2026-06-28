# 5. Component Deep Dive

This section details every code component.

### Source (`src/`)

- **`agents/`**
  - **`graph.py`**: Compiles the DAG workflow utilizing `StateGraph`. Defines the sequential order `Support -> Regime -> Strategy -> Validation -> Risk`. Contains conditional edge functions that drop out early.
  - **`state.py`**: TypedDict for state schema definition. 
  - **`market_regime.py`**: Defines `market_regime_node`. Evaluates volatility and current trends.
  - **`prediction.py`**, **`sentiment.py`**, **`news_analyst.py`**: Analyzes market sentiment computationally and semantically. 
  - **`strategy_selection.py`**: Maps regime to deterministic functions.
  - **`signal_validation.py`**: Vetoes math indicators utilizing LLM intuition.
  - **`risk_compliance.py`**: Sizing trades, blocking unsafe trades, ensuring the kill switch logic runs.

- **`api/`**
  - **`health.py`**: Contains simple fast-API or wrapper checks handling liveliness probes useful if deployed in dockerized setups.

- **`backtesting/`**
  - **`engine.py`**: Feeds CSV historical data back into the `TradingState` loop efficiently, stripping out the API wait delays.
  - **`strategies.py`**: The baseline strategy configuration files used to anchor Agentic deviations.

- **`config/`**
  - **`settings.py`**: Uses Pydantic to read and validate ENV variables (DB credentials, API keys, Model preferences).

- **`dashboard/`**
  - **`cli.py`**: Utilizes the `rich` library to render a terminal based dashboard providing live updates of the Agent Graph processing.

- **`execution/`**
  - **`service.py`**: `ExecutionService` — the single mode-switched entry point for order
    submission. Adds **order idempotency** (a persisted `IdempotencyStore`), a **shadow mode**
    (mirror live, send nothing), and no-silent-downgrade mode resolution.
  - **`live_executor.py`**: `LiveBrokerExecutor` (submit + poll status to a terminal fill) and
    `reconcile_positions` (local vs broker, broker = source of truth). Gated behind
    `allow_live_orders`.
  - **`costs.py`**: `CostModel` — slippage + NSE-style fees applied to paper fills.
  - **`adapter.py`**: DhanHQ / local broker adapters (lazy DhanHQ import).
  - **`paper_engine.py`**: Simulated exchange — cost-aware fills, long/short/partial accounting,
    **atomic, crash-safe** state persistence.
  - **`exit_manager.py`**: Trailing stops / time / partial / regime exits, with MAE/MFE tracking;
    state **persisted across restarts**.
  - **`journal.py`**: Durable trade history (SQLAlchemy); records net P&L + partial exits.

- **`finops/`**
  - **`cost_tracker.py`**: Per-agent, per-IST-day Groq token + cost accounting with daily
    budgets and a `budget_status()` used as a spend kill-switch.
  - **`alerts.py`**: `AlertManager` — logs + best-effort Telegram, de-duped per day (budget,
    drawdown, data-staleness, ...).

- **`profit/`**
  - **`goal_engine.py`**: Turns a monthly return target into a **risk-bounded plan** (required
    win-rate / trade frequency) and an on/off-pace tracker. Advisory only — never relaxes risk.

- **`market/`**
  - Obtains the data and computes technicals: Live/WebSocket (`live_data.py`,
    `websocket_feed.py`), YFinance (`yfinance_feed.py`), simulated (`simulated_data.py`), the
    `MarketDataManager`, `indicators.py` (+ `IndicatorCache`), `signals.py` (evidence-based
    confidence), `sizing.py` (risk-based `PositionSizer`), `stock_discovery.py`, and
    `history_manager.py`.

- **`memory/`**
  - The **closed** learning loop: `analyzer.py` (`compute_outcome`), `classifier.py` (mistake →
    lesson), `database.py` (storage + unified `decayed_score`), `injection.py` (inject lessons),
    `feedback.py` (wires close → lesson + marks lessons useful), `performance_tracker.py`
    (persisted per-strategy win-rates).

- **`utils/`**
  - Safety/reliability toolkit: circuit breaker, rate limiter, TTL cache, custom errors,
    events, and **`market_time.py`** (fixed UTC+05:30 IST helpers).

### Scripts (`scripts/`)
These act as the `main` entrypoints for users.
- `setup.py`: **Guided one-command setup** — creates `.env`, checks keys, prints a readiness checklist.
- `run_live_trading.py`: The main app — connects live/simulated data to the compiled LangGraph
  and drives the `rich` dashboard, execution, exits, journaling, FinOps and the learning loop.
- `diagnose_risk.py`, `check_config.py`, `test_dhan_connection.py`: Utility scripts validating components without firing LLMs.
