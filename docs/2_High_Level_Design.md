# 2. High-Level Design (HLD)

## Architecture Overview

The TradingAgent follows a decoupled, layered micro-architecture pattern orchestrating AI via LangGraph state machines. The architecture comprises five primary components:

### 1. Market Data & Signal Layer (Deterministic)
- **Data Ingestion:** YFinance, WebSockets, simulated and historical datasets.
- **Indicators Engine:** Computes EMA, RSI, MACD using standard quantitative libraries (like `ta`).
- **Baseline Signaler:** Generates simple deterministic cross-over/breakout signals.

### 2. Agentic Decision Layer (LLM Orchestration)
Powered by **LangGraph**, it maintains a global `TradingState`.
- **Pre-analysis Branch:** News Analyst, Sentiment, Prediction algorithms (run in parallel).
- **Core Pipeline Branch:** 
  1. Market Regime Agent
  2. Strategy Selection Agent
  3. Signal Validation Agent
  4. Risk & Compliance Agent

### 3. Memory & Feedback Intelligence Layer
- **Post-Trade Analyzer:** Assesses PnL and trade characteristics.
- **Memory Injection:** Synthesizes LLM lessons about strategy failures and saves them into the vector database. When the next trading cycle runs, it searches the Vector DB for lessons semantically similar to current parameters to inject into the `TradingState`.

### 4. Execution & Order Management Layer
- **Paper Trading Engine:** Bridges the Risk agent's output with simulated broker executions.
- **Dhan API Adapter:** Can be toggled for live/forward testing connections.
- **Trade Journal DB:** PostgreSQL-backed permanent storage of every decision state alongside the final financial result.

### 5. Observability & Tracing Layer
- Integrated with **LangSmith**.
- Every agent invocation, input context, LLM output parsed as JSON, and graph transition is recorded metadata-tagged. Allows playback and deep introspection to debug "Why did the system take this trade?".

## Context Flow Diagram

```text
[ Market Data Source ] --> [ Technical Indicators / Raw Signals ]
                                      |
                                      v
                      [ SUPPORT AGENTS (News, Predict, Sentiment) ]
                                      |
                                      v
                          [ MARKET REGIME AGENT ]   <-- (Memory Context Injected)
                                      |
                                      v
                        [ STRATEGY SELECTION AGENT ]<-- (Performance Feedback)
                                      |
                                      v
                         [ SIGNAL VALIDATION AGENT ]
                                      |
                                      v
                        [ RISK & COMPLIANCE AGENT ]
                                      |
                                      v
                      [ PAPER EXECUTION / BROKER API ]
                                      |
                                      v
                         [ TRADE JOURNAL DATABASE ]
                                      |
                                      v
                        [ MISTAKE CLASSIFIER / MEMORY ] --> (Loops back to start)
```
