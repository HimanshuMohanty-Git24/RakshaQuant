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
  - **`adapter.py`**: Abstract Broker Interface.
  - **`paper_engine.py`**: Simulated exchange. Tracks portfolio balance precisely.
  - **`exit_manager.py`**: Handles checking trailing stops, take profits independent of the LLMs.
  - **`journal.py`**: PostgreSQL serialization logging what was bought/sold and why.

- **`market/`**
  - Handles the universe of obtaining the numbers. From Live Data pulling (`live_data.py`), to YFinance (`yfinance_feed.py`), and managing standard technicals (`indicators.py`). Sizing rules and stock screeners are managed here (`stock_discovery.py`).

- **`memory/`**
  - Core component for the learning loop. Trade analyzers (`analyzer.py`), categorizing mistakes (`classifier.py`), hooking to DB (`database.py`), and inserting knowledge (`injection.py`). 

- **`utils/`**
  - Safety and reliability toolkit. Circuit Breakers, Rate Limiting to not annoy APIs, Caching for deduplication context, custom Error classes.

### Scripts (`scripts/`)
These act as the `main` entrypoints for users.
- `run_live_trading.py` / `run_trading.py`: Kicks off the orchestration pipeline connecting the live data feeds to the compiled LangGraph.
- `run_with_dashboard.py`: Starts the runner alongside the Rich dashboard.
- `diagnose_risk.py`, `check_config.py`, `test_dhan_connection.py`: Simple utility scripts validating components without firing LLMs.
