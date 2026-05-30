# 3. Low-Level Design (LLD)

This section dissects the code layout and core module interactions.

## Core Directories & Responsibilities

### `src/agents/`
Contains the LangGraph graph logic and all agent implementations.
- **`graph.py`**: Compiles the `StateGraph`. Implements `support_agents_node` wrapper and handles workflow conditional edges.
- **`state.py`**: Defines the `TradingState` TypedDict containing all keys explicitly updated through the pipeline (e.g., `regime`, `validated_signals`, `portfolio_value`).
- **`*_node.py` / Agent Files**: Each encapsulates an LLM prompt and structure defining what it expects as inputs and what JSON it must output. 

### `src/backtesting/`
- **`engine.py`**: Runs a simulation looping through historical OHLCV. Instead of sending orders to the paper trader, checks hypothetical fill prices and stores mock performance.
- **`strategies.py`**: Contains deterministic rules to backtest against the agents.

### `src/execution/`
- **`paper_engine.py`**: Acts as a stateful order book. Processes limit/market orders, updates simulated cash/margin.
- **`adapter.py`**: Abstract interface implementations for integrating real brokers (e.g., DhanHQ).
- **`journal.py`**: Synchronizes with a PostgreSQL database schema to store trade lifecycle events.

### `src/market/`
- **`data_feed.py` / `yfinance_feed.py` / `websocket_feed.py`**: Strategies and abstractions for pulling ticker data. Adapts asynchronous streams buffer to synchronous or batched states for LangGraph.
- **`indicators.py` / `signals.py`**: The quantitative math core running standard TA logic over pandas dataframes.

### `src/memory/`
- **`database.py`**: Stores raw memories into Vector DB or Postgres representations.
- **`analyzer.py` / `classifier.py`**: Contains logic extracting features (Win/Loss, drawdown timeframe) and asking an LLM to categorize the mistake string, storing it into the `database.py`.
- **`injection.py`**: Runs before the main decision agents logic to query the database, mapping past notes formatted to insert into the LangGraph state.

### `src/utils/`
- **`circuit_breaker.py`**: State machine (Open, Half-Open, Closed) preventing API spam to the broker or LLM endpoint in case of 5xx errors.
- **`rate_limiter.py`**: Controls request throughput matching Groq/Broker tier constraints.

## Class Interaction & Interface Example

**Agent Invocation Pattern**
1. The `StateGraph` runner calls the node function (e.g., `market_regime_node(state)`).
2. The node formats prompt utilizing `langchain_core`.
3. It binds Pydantic objects using `.with_structured_output(RegimeSchema)` mapped to our requested LLM.
4. Extracted structured dict is appended and returned strictly to mutate the master `TradingState`.
