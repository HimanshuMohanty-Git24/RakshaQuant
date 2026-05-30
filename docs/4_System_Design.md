# 4. System Design & Patterns

## How It Thinks (Agent Cognitive Strategy)
The system leverages **Multi-Agent Orchestration (Ensemble AI)**. Instead of one large prompt attempting to evaluate news, regime, risk, and math in one go, the task is separated. 
- **Support Agents** think horizontally: They just care about summarizing raw real-world data points effectively.
- **Decision Agents** think vertically: Strategy Selection is only asked to act as a PM. Risk Compliance is only asked to act as a harsh enforcer. 
The restriction forces the LLM to output highly bounded, confident outputs structured strictly as JSON.

## Design Patterns Used

1. **State Machine / Blackboard Pattern**
   Implemented via LangGraph. The `TradingState` acts as a Central Blackboard where specialized agents post their inferences for others to read.
   
2. **Strategy Pattern**
   `Execution Adapter` and `Data Feed` conform to strict interface boundaries so the application can swap from `yfinance_feed` to `websocket_feed`, and `paper_engine` to `live_broker_adapter` seamlessly.

3. **Circuit Breaker**
   Found in `utils/circuit_breaker.py`. Protects LLM endpoint and Broker endpoints from cascading failures if a provider goes offline.

4. **Repository & Singleton Patterns**
   Used extensively to manage connections to the Database (`memory/database.py`) and memory components.

## Fault Tolerance & Safety

- **LLM Output Safety**: The use of `with_structured_output` inside Langchain combined with Pydantic ensures the LLM's response matches exactly the schema we require (or forces a retry/fail).
- **Graceful Degradation**: As observed in `graph.py`, if a Support Agent fails (e.g. news website changed layout, API down), it gracefully logs a non-fatal warning and returns empty dictionaries allowing the rest of the node (Regime, Risk) to execute safely on mathematical indicators.
- **Kill Switches**: Built permanently into Risk Control. `check_kill_switch(state)` ensures that anomalous market conditions instantly drop the state to the `END` node, halting the graph. Daily loss caps inherently guarantee capital preservation.
- **No LLM in Hot Path**: Time-critical trading is executed via deterministic rules once authorized. LLMs run asynchronously and occasionally, limiting latency constraints.
