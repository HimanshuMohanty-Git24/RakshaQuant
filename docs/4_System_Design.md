# 4. System Design & Patterns

> **📌 v2.1 hardening note.** Beyond the cognitive patterns described here, the system now closes its learn-from-losses loop at runtime, derives **evidence-based** signal confidence and **risk-based** position sizing, tracks LLM cost against daily budgets (FinOps), and runs all execution through a unified `ExecutionService` with order idempotency and a safe **shadow mode** (real broker orders only with `ALLOW_LIVE_ORDERS=true`). See README "What's New (v2.1)".


## How It Thinks: Agent Cognitive Strategy

The TradingAgent leverages **Multi-Agent Orchestration (Ensemble AI)** to decompose complex trading decisions into specialized, bounded subtasks. Instead of one large prompt attempting to evaluate news, regime, risk, and math in one go, the work is divided among focused agents:

### Separation of Concerns

```mermaid
flowchart LR
    subgraph "Horizontal Thinking (Support Agents)"
        direction TB
        N["📰 News Analyst<br/><i>'What happened in<br/>the market today?'</i>"]
        S["🎭 Sentiment Agent<br/><i>'What is the collective<br/>market mood?'</i>"]
        P["🔮 Prediction Agent<br/><i>'Where might prices<br/>go next?'</i>"]
    end

    subgraph "Vertical Thinking (Decision Agents)"
        direction TB
        MR["🏔️ Market Regime<br/><i>'What type of market<br/>are we in?'</i>"]
        SS["📋 Strategy Selection<br/><i>'Which strategies<br/>should we deploy?'</i>"]
        SV["✅ Signal Validation<br/><i>'Which signals are<br/>worth trading?'</i>"]
        RC["🛡️ Risk & Compliance<br/><i>'Is this trade safe<br/>to execute?'</i>"]
    end

    subgraph "Key Principle"
        P1["Each agent has a SINGLE,<br/>well-defined role"]
        P2["Outputs are BOUNDED<br/>structured JSON"]
        P3["Failures are ISOLATED<br/>with graceful fallbacks"]
    end

    N & S & P -->|"Enrich context"| MR
    MR --> SS --> SV --> RC
```

**Support Agents** think **horizontally** — they only care about summarizing raw real-world data points effectively. They gather information without making trading decisions.

**Decision Agents** think **vertically** — each is asked to play a specific role:
- **Strategy Selection** acts as a **Portfolio Manager** choosing strategies
- **Signal Validation** acts as a **Senior Trader** filtering signals
- **Risk Compliance** acts as a **Risk Manager** enforcing rules

This restriction forces the LLM to output highly bounded, confident outputs structured strictly as JSON.

---

## Design Patterns Used

### 1. State Machine / Blackboard Pattern

```mermaid
flowchart TD
    subgraph "Blackboard Pattern (TradingState)"
        BB["Central TradingState<br/>(TypedDict)"]
    end

    SA["Support Agents<br/>📝 Write: news_sentiment,<br/>market_mood, prediction_signals"]
    MR["Market Regime Agent<br/>📝 Write: regime,<br/>regime_confidence"]
    SS["Strategy Selection<br/>📝 Write: active_strategies,<br/>strategy_reasoning"]
    SV["Signal Validation<br/>📝 Write: validated_signals,<br/>rejected_signals"]
    RC["Risk Agent<br/>📝 Write: approved_trades,<br/>risk_warnings"]

    SA -->|"Read: market_data"| BB
    SA -->|"Write: sentiment data"| BB
    MR -->|"Read: sentiment + indicators"| BB
    MR -->|"Write: regime"| BB
    SS -->|"Read: regime"| BB
    SS -->|"Write: strategies"| BB
    SV -->|"Read: regime + strategies + predictions"| BB
    SV -->|"Write: validated signals"| BB
    RC -->|"Read: validated signals"| BB
    RC -->|"Write: approved trades"| BB
```

Implemented via **LangGraph**. The `TradingState` acts as a **Central Blackboard** where specialized agents post their inferences for others to read. This pattern ensures:
- **Decoupled communication** — agents don't know about each other, only the shared state
- **Incremental enrichment** — each agent adds to the state
- **Traceable decisions** — the full state is captured for every workflow

---

### 2. Strategy Pattern

```mermaid
classDiagram
    class DataFeed {
        <<interface>>
        +get_data(symbol) DataFrame
        +get_historical(symbol) DataFrame
    }

    class YFinanceFeed {
        +get_data(symbol) DataFrame
        +get_historical(symbol) DataFrame
    }

    class WebSocketFeed {
        +connect()
        +subscribe(symbol)
        +listen()
    }

    class SimulatedFeed {
        +get_data(symbol) DataFrame
        +generate_ohlcv(symbol) DataFrame
    }

    DataFeed <|.. YFinanceFeed
    DataFeed <|.. WebSocketFeed
    DataFeed <|.. SimulatedFeed

    class ExecutionEngine {
        <<interface>>
        +place_order(request) OrderResult
        +get_positions() list
    }

    class LocalPaperEngine {
        +place_order(symbol, side, qty, price) Order
        +get_balance() float
    }

    class DhanAdapter {
        +place_order(request) OrderResult
        +cancel_order(order_id) bool
    }

    ExecutionEngine <|.. LocalPaperEngine
    ExecutionEngine <|.. DhanAdapter

    note for DataFeed "Swap from yfinance_feed\nto websocket_feed\nseamlessly"

    note for ExecutionEngine "Swap from paper_engine\nto live_broker_adapter\nseamlessly"
```

The `Execution Adapter` and `Data Feed` conform to strict interface boundaries, allowing seamless swapping:
- `yfinance_feed` ↔ `websocket_feed` ↔ `simulated_data` (for market data)
- `paper_engine` ↔ `dhan_adapter` (for order execution)

---

### 3. Circuit Breaker Pattern

```mermaid
stateDiagram-v2
    [*] --> Closed : Initial State

    state Closed {
        [*] --> Monitoring
        Monitoring --> Monitoring : success (reset counter)
        Monitoring --> FailureTracking : failure
        FailureTracking --> FailureTracking : failure (increment)
    }

    Closed --> Open : failures >= threshold (5)

    state Open {
        [*] --> Rejecting
        Rejecting : All requests raise\nCircuitBreakerOpenError
    }

    Open --> HalfOpen : recovery_time elapsed (60s)

    state HalfOpen {
        [*] --> Testing
        Testing : Allow ONE test request
    }

    HalfOpen --> Closed : test SUCCESS
    HalfOpen --> Open : test FAILURE

    note right of Closed
        Normal operation
        Failures tracked
    end note

    note right of Open
        Service protected
        Timer running
    end note

    note right of HalfOpen
        Cautious probe
        One request allowed
    end note
```

Found in `utils/circuit_breaker.py`. Three dedicated circuit breakers protect different external services:

| Circuit Breaker | Threshold | Recovery | Protects |
|---|---|---|---|
| `groq_circuit_breaker` | 3 failures | 30 seconds | LLM endpoint (Groq API) |
| `broker_circuit_breaker` | 5 failures | 60 seconds | DhanHQ broker API |
| `market_data_circuit_breaker` | 5 failures | 30 seconds | Market data feeds |

---

### 4. Token Bucket Rate Limiter

```mermaid
flowchart TD
    REQ["Incoming API Request"]
    BUCKET{"Token Bucket<br/>(30 tokens/min)"}
    
    REQ --> BUCKET
    BUCKET -->|"Token available"| CONSUME["✅ Consume token<br/>Process request"]
    BUCKET -->|"No tokens"| WAIT["⏳ Wait for refill<br/>(exponential backoff)"]
    WAIT --> BUCKET
    
    REFILL["⏱️ Continuous Refill<br/>0.5 tokens/second"] -.-> BUCKET
    
    subgraph "Backoff Strategy"
        B1["Attempt 1: 2.0s"]
        B2["Attempt 2: 4.0s"]
        B3["Attempt 3: 8.0s"]
        B1 --> B2 --> B3
    end
    
    WAIT --> B1
```

Controls request throughput matching Groq free tier constraints (30 RPM). Uses exponential backoff with jitter on rate limit errors.

---

### 5. Event-Driven Architecture (Pub/Sub)

```mermaid
flowchart LR
    subgraph Publishers
        MKT["Market Data"]
        AGT["Agent Pipeline"]
        EXE["Execution Engine"]
        SYS["System Events"]
    end

    subgraph "Event Bus"
        EB["EventBus<br/>(Global Singleton)<br/>20 Event Types"]
    end

    subgraph Subscribers
        DASH["Dashboard"]
        TG["Telegram"]
        LOG["Logging"]
        MEM["Memory System"]
    end

    MKT -->|"QUOTE_UPDATE<br/>INDICATOR_UPDATE<br/>SIGNAL_GENERATED"| EB
    AGT -->|"REGIME_CLASSIFIED<br/>STRATEGY_SELECTED<br/>SIGNAL_VALIDATED<br/>TRADE_APPROVED"| EB
    EXE -->|"ORDER_PLACED<br/>ORDER_FILLED<br/>POSITION_OPENED<br/>POSITION_CLOSED"| EB
    SYS -->|"CYCLE_STARTED<br/>CYCLE_COMPLETED<br/>ERROR_OCCURRED<br/>KILL_SWITCH_TRIGGERED"| EB

    EB -->|subscribe| DASH
    EB -->|subscribe| TG
    EB -->|subscribe| LOG
    EB -->|subscribe| MEM
```

### 6. Repository & Singleton Patterns

Used extensively to manage connections and stateful resources:

| Singleton | Access Function | Purpose |
|---|---|---|
| `Settings` | `get_settings()` | Application configuration (LRU cached) |
| `RateLimiter` | `get_groq_limiter()` | Groq API rate limiting |
| `CircuitBreaker` | `get_groq_circuit_breaker()` | LLM circuit breaker |
| `EventBus` | `get_event_bus()` | Global event bus |
| `PerformanceTracker` | `get_performance_tracker()` | Strategy performance tracking |
| `MemoryDecayScheduler` | `get_memory_scheduler()` | Memory maintenance scheduler |
| `TelegramNotifier` | `get_notifier()` | Telegram notification service |

---

## Fault Tolerance & Safety

### LLM Output Safety

```mermaid
flowchart TD
    LLM["LLM Response<br/>(raw text)"]
    
    STRIP["Strip markdown<br/>code blocks"]
    PARSE["JSON.loads()"]
    VALIDATE["Validate against<br/>expected schema"]
    CLAMP["Clamp values<br/>(confidence 0-1)"]
    ENUM["Validate enum values<br/>(regime, decision type)"]
    SUCCESS["✅ Valid structured output"]
    
    FALLBACK["⚠️ Rule-based fallback<br/>(deterministic)"]
    
    LLM --> STRIP --> PARSE
    PARSE -->|"Valid JSON"| VALIDATE --> CLAMP --> ENUM --> SUCCESS
    PARSE -->|"Parse error"| FALLBACK
    VALIDATE -->|"Schema mismatch"| FALLBACK
```

The use of structured prompts and response parsing ensures LLM outputs match exactly the schema required. Every agent has a **deterministic fallback** that activates on:
- JSON parse errors
- Schema validation failures
- Rate limit errors
- Circuit breaker open state
- Network timeouts

### Graceful Degradation Hierarchy

```mermaid
flowchart TD
    NORMAL["Normal Operation<br/>(All systems green)"]
    
    LLM_FAIL["LLM Rate Limited"]
    NEWS_FAIL["News Feed Down"]
    DB_FAIL["Database Unavailable"]
    BROKER_FAIL["Broker API Down"]
    
    FALLBACK_MODEL["Use Fallback Model<br/>(Llama-8B-Instant)"]
    RULE_BASED["Use Rule-Based Logic"]
    EMPTY_SENTIMENT["Empty Sentiment<br/>(continue with indicators)"]
    SQLITE["Fallback to SQLite<br/>(in-memory)"]
    PAPER["Fallback to Paper Engine"]
    
    NORMAL -->|"Groq 429"| LLM_FAIL --> FALLBACK_MODEL
    FALLBACK_MODEL -->|"Also fails"| RULE_BASED
    NORMAL -->|"RSS error"| NEWS_FAIL --> EMPTY_SENTIMENT
    NORMAL -->|"PostgreSQL down"| DB_FAIL --> SQLITE
    NORMAL -->|"Dhan API down"| BROKER_FAIL --> PAPER
```

As observed in `graph.py`, if a Support Agent fails (e.g., news website changed layout, API down), it gracefully logs a **non-fatal warning** and returns empty dictionaries, allowing the rest of the pipeline to execute safely on mathematical indicators.

---

### Kill Switches & Safety Mechanisms

```mermaid
flowchart TD
    subgraph "Kill Switch Triggers"
        DL["Daily Loss ≥ ₹10,000"]
        DD["Drawdown ≥ 5%"]
        LC["Low Confidence < 0.3"]
        TH["Outside Trading Hours<br/>(before 09:15 or after 15:15)"]
    end

    subgraph "Safety Actions"
        ABORT["🛑 Abort Trading Cycle<br/>(graph → END node)"]
        REJECT["🚫 Reject All Trades"]
        LOG["📋 Log CRITICAL event"]
        NOTIFY["📱 Telegram Alert"]
    end

    DL --> ABORT & LOG & NOTIFY
    DD --> ABORT & LOG & NOTIFY
    LC --> ABORT & LOG
    TH --> REJECT & LOG
```

**Built permanently into Risk Control:**
- `check_kill_switch(state)` ensures anomalous market conditions instantly route the state to the `END` node
- Daily loss caps guarantee capital preservation
- Trading hours enforcement prevents off-market trades
- Maximum position count prevents over-diversification

### No LLM in Hot Path

```mermaid
flowchart LR
    subgraph "Slow Path (LLM)"
        SA["Support Agents<br/>(async, occasional)"]
        MR["Market Regime<br/>(periodic)"]
        SS["Strategy Selection<br/>(periodic)"]
        SV["Signal Validation<br/>(per cycle)"]
    end

    subgraph "Fast Path (Deterministic)"
        RC["Risk Compliance<br/>(rule-based)"]
        EM["Exit Manager<br/>(trailing stops)"]
        PE["Paper Engine<br/>(order fill)"]
        SIG["Signal Generation<br/>(indicators)"]
    end

    SA & MR & SS & SV -->|"Decisions made<br/>asynchronously"| RC
    RC --> EM --> PE
    SIG -->|"Pure math"| SV
```

Time-critical trading operations (order execution, stop-loss checks, position sizing) are executed via deterministic rules once authorized. LLMs run asynchronously and occasionally, limiting latency constraints.

---

## Configuration Architecture

### Environment Variable Flow

```mermaid
flowchart TD
    ENV[".env File"]
    OS["OS Environment<br/>Variables"]
    
    PY["Pydantic Settings<br/>(Settings class)"]
    VAL["Cross-Field Validation<br/>(@model_validator)"]
    CACHE["LRU Cache<br/>(get_settings())"]
    
    ENV --> PY
    OS --> PY
    PY --> VAL --> CACHE
    
    CACHE --> AGENTS["Agent Config<br/>(model, temp, tokens)"]
    CACHE --> BROKER["Broker Config<br/>(API keys, URLs)"]
    CACHE --> RISK["Risk Config<br/>(limits, thresholds)"]
    CACHE --> MARKET["Market Config<br/>(hours, data source)"]
    CACHE --> INFRA["Infrastructure<br/>(cache TTL, rate limits)"]
```

### Configuration Groups

| Group | Key Settings | Default |
|---|---|---|
| **LLM** | `groq_model_primary`, `groq_temperature` | `llama-3.3-70b-versatile`, `0.1` |
| **Broker** | `dhan_client_id`, `execution_mode` | None, `local_paper` |
| **Trading** | `max_daily_trades`, `daily_loss_limit` | 50, ₹10,000 |
| **Position** | `max_position_pct`, `risk_per_trade` | 10%, 2% |
| **Memory** | `memory_top_n_lessons`, `memory_decay_days` | 5, 30 |
| **Market** | `market_open_time`, `market_close_time` | 09:15, 15:30 |
| **Rate Limit** | `groq_requests_per_minute` | 30 |
| **Cache** | `cache_news_ttl`, `cache_quotes_ttl` | 300s, 60s |

### Validation Rules

| Rule | Check | Action |
|---|---|---|
| Live trading | Requires Dhan credentials | Warning logged |
| Dhan execution | Should use Dhan data source | Warning logged |
| Risk parameters | `risk_per_trade ≤ max_position_pct` | Warning logged |
| Total risk | `max_total_risk ≥ risk_per_trade` | Warning logged |
| Market hours | `open_time < close_time` | Warning logged |
| Trading window | `no_trading_before < no_trading_after` | Warning logged |
| Telegram | Both token and chat_id required | Warning logged |

---

## Observability Architecture

### LangSmith Integration

```mermaid
flowchart TD
    subgraph "Tracing Setup"
        ENV["Environment Variables<br/>LANGSMITH_API_KEY<br/>LANGSMITH_PROJECT<br/>LANGSMITH_TRACING_V2"]
        SETUP["setup_tracing()<br/>Validates connection"]
    end

    subgraph "Per-Workflow Tracing"
        CTX["trading_trace(workflow_id)<br/>Context Manager"]
        DEC["@trace_agent(name)<br/>Decorator"]
        META["add_trace_metadata()<br/>Runtime metadata"]
        TAG["tag_trace()<br/>Trade-specific tags"]
    end

    subgraph "Callback System"
        CB["TracingCallback"]
        START["on_agent_start()"]
        END_CB["on_agent_end()"]
        DECISION["on_decision()"]
        ERROR_CB["on_error()"]
    end

    subgraph "LangSmith Dashboard"
        REPLAY["Decision Replay"]
        PERF["Performance Metrics"]
        DEBUG["Debug 'Why did the<br/>system take this trade?'"]
    end

    ENV --> SETUP --> CTX
    CTX --> DEC & META & TAG
    DEC --> CB
    CB --> START & END_CB & DECISION & ERROR_CB
    CB --> REPLAY & PERF & DEBUG
```

Every agent invocation, input context, LLM output, and graph transition is recorded with metadata tags. This allows:
- **Playback** — replay any trading cycle step by step
- **Deep introspection** — answer "Why did the system take this trade?"
- **Performance monitoring** — track latency, token usage, decision quality
- **Error debugging** — trace failures through the full call chain

### Notification System

```mermaid
classDiagram
    class TelegramNotifier {
        -bot_token: str
        -chat_id: str
        -enabled: bool
        +send_message(text) bool
        +send_trade_alert(symbol, side, qty, price, strategy, confidence) bool
        +send_discovery_alert(stocks) bool
        +send_pnl_summary(balance, pnl, ...) bool
        +send_sentiment_alert(mood_index, mood_label) bool
        +send_error_alert(error, context) bool
        +send_startup_message() bool
        +send_shutdown_message(reason) bool
    }

    note for TelegramNotifier "API: https://api.telegram.org/bot{token}/{method}\nUses aiohttp for async HTTP\nAll methods return bool (success/failure)\nGracefully handles missing credentials"
```

---

## Health Check Architecture

```mermaid
flowchart LR
    subgraph "Fast Checks (always run)"
        MEM["Memory System<br/>(AgentMemoryDB)"]
        CB["Circuit Breakers<br/>(all instances)"]
        PW["Paper Wallet<br/>(balance + positions)"]
    end

    subgraph "Slow Checks (optional)"
        DB["Database<br/>(SELECT 1)"]
        GROQ["Groq API<br/>(test prompt)"]
        MKTD["Market Data<br/>(fetch RELIANCE)"]
    end

    HC["health_check()<br/>(async)"]
    
    HC --> MEM & CB & PW
    HC -->|"include_slow_checks=True"| DB & GROQ & MKTD

    subgraph "Status Resolution"
        ANY_UNHEALTHY["Any UNHEALTHY?"] -->|Yes| UNHEALTHY["🔴 UNHEALTHY"]
        ANY_UNHEALTHY -->|No| ANY_DEGRADED["Any DEGRADED?"]
        ANY_DEGRADED -->|Yes| DEGRADED["🟡 DEGRADED"]
        ANY_DEGRADED -->|No| HEALTHY["🟢 HEALTHY"]
    end

    MEM & CB & PW & DB & GROQ & MKTD --> ANY_UNHEALTHY
```

---

## Dashboard Architecture

### Rich CLI Terminal Dashboard

```mermaid
graph TD
    subgraph "Dashboard Layout (Rich Library)"
        direction TB
        HEADER["🔝 Header Panel<br/>Branding + Mode Badges + Session Time"]
        
        subgraph "Row 1 (3 columns)"
            ACCOUNT["💰 Account Panel<br/>Balance, P&L, Best/Worst"]
            TRADES["📊 Trades Panel<br/>Win/Loss, Win Rate Bar"]
            REGIME["🏔️ Regime Panel<br/>Regime + Confidence + Strategies"]
        end
        
        subgraph "Row 2 (3 columns)"
            MARKET["📈 Market Overview<br/>Top 8 Stocks Table"]
            DECISION["🎯 Decision Panel<br/>Current Signal + Reasoning"]
            AGENTS["🤖 Agent Stats<br/>Cycles, Signals, Validations"]
        end
        
        subgraph "Row 3 (2 columns)"
            POSITIONS["📋 Positions Panel<br/>Open Positions Table (max 5)"]
            ACTIVITY["📜 Activity Log<br/>Scrolling Log (last 10)"]
        end
    end

    HEADER --- ACCOUNT & TRADES & REGIME
    ACCOUNT & TRADES & REGIME --- MARKET & DECISION & AGENTS
    MARKET & DECISION & AGENTS --- POSITIONS & ACTIVITY
```

**Regime Color Coding:**

| Regime | Color | Icon |
|---|---|---|
| `trending_up` | Green | 📈 |
| `trending_down` | Red | 📉 |
| `ranging` | Yellow | ↔️ |
| `volatile` | Magenta | ⚡ |
| `unknown` | Grey | ❓ |
