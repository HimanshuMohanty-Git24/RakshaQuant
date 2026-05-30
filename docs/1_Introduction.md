# 1. Introduction

## Overview
The **TradingAgent** is an Agentic Paper Trading System built for the Indian National Stock Exchange (NSE). Traditional algorithmic trading systems rely on deterministic, hard-coded rules and fail to factor in nuance. In contrast, this project separates *thinking (decision-making)* from *execution*, modeling real-world trading desk roles (Portfolio Manager, Trader, Risk Manager, Analyst) through an ensemble of AI Agents.

The core motivation is to implement a **learning feedback loop** combined with **full observability**, allowing the AI to learn from post-trade outcomes without retraining the underlying language models.

## How It Works: Start to End

1. **Market Data & Pre-processing (Deterministic)**
   The system ingests real-time or historical OHLCV data using providers like YFinance or DhanHQ APIs. It computes technical indicators and identifies baseline signals deterministically to feed into the agents.

2. **Support Agents Execution (Information Gathering)**
   Whenever a trading cycle runs, the *Support Agents* wake up:
   - **News Analyst:** Scrapes recent financial news, computes sentiment.
   - **Sentiment Agent:** Aggregates market mood.
   - **Prediction Agent:** Runs predictive models to guess the price target/direction over the short term.
   These agents enrich the state context dictionary for downstream decision makers.

3. **Market Regime Detection**
   The *Market Regime Agent* evaluates the market stats, volatility, and the support agents' findings to classify the current market environment (e.g., Bull, Bear, Sideways, High Volatility). If confidence is too low or a kill switch is triggered, the cycle immediately aborts.

4. **Strategy Selection**
   Armed with the knowledge of the current market regime, the *Strategy Selection Agent* decides which particular trading strategies to deploy for this cycle. It looks at historical agent memory to adapt to what worked previously in similar regimes.

5. **Signal Validation**
   The deterministic signals generated initially by our technical indicators are parsed by the *Signal Validation Agent*. It evaluates them against the chosen strategies, the regime, the predictions, and past agent memory lessons. It accepts valid signals and discards weak ones. If no signals pass, the cycle terminates.

6. **Risk & Compliance Check**
   Before execution, the *Risk & Compliance Agent* acts as the firm's strict gatekeeper. It checks capital exposure limits, drawdown rules, and trade frequency constraints. It assigns appropriate positions sizing and sets tight stop losses.

7. **Execution & Journaling**
   The approved orders are finally forwarded to the *Execution Adapter* (in our case, the Paper Engine). Trades are logged meticulously in a *Trade Journal* database setup.

8. **Feedback & Learning Loop**
   Post-trade, the system analyzes whether the trade resulted in a win or a loss (computing MAE/MFE). The *Mistake Classifier* derives actionable lessons (e.g., "Overtrading in a choppy regime"). These insights are injected back into the Agents' memory (Pinecone/Postgres with pgvector) to avoid repeating mistakes during the next cycle.
