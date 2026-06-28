"""
RakshaQuant Live Trading Dashboard

Enhanced with:
- Real historical indicator calculation (replaces fabricated indicators)
- Real trade execution via paper engine (replaces random P&L)
- Dynamic exit management (trailing stops, time exits, partial profits)
- Performance tracking for strategy learning
"""

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.live import Live

from src.config import get_settings
from src.agents.graph import create_trading_graph, run_trading_cycle
from src.agents.risk_compliance import check_kill_switch
from src.finops import get_alert_manager, get_cost_tracker
from src.notifications.telegram import get_notifier
from src.profit import ProfitGoalEngine
from src.market.manager import MarketDataManager, MarketQuote, is_market_open
from src.market.history_manager import HistoryManager
from src.market.indicators import Timeframe, get_indicator_cache
from src.market.signals import SignalEngine
from src.market.stock_discovery import StockDiscovery
from src.memory.database import AgentMemoryDB
from src.memory.performance_tracker import get_performance_tracker
from src.memory.classifier import MistakeClassifier
from src.memory.injection import MemoryInjector
from src.memory import feedback
from src.execution.paper_engine import LocalPaperEngine
from src.execution.exit_manager import ExitManager
from src.execution.service import ExecutionService, IdempotencyStore
from src.observability.tracing import setup_tracing
from src.dashboard.cli import TradingDashboard, create_dashboard_layout

console = Console()
logger = logging.getLogger(__name__)


def calculate_real_indicators(history_manager: HistoryManager, symbol: str):
    """
    Calculate REAL indicators from historical data.

    Computes on settled bars only (dropping the still-forming current-day bar) when
    ``signals_exclude_forming_bar`` is set, to avoid intra-bar repainting / look-ahead,
    and memoizes via the shared IndicatorCache so unchanged data is not recomputed
    every cycle.
    """
    settings = get_settings()
    include_forming = not settings.signals_exclude_forming_bar
    df = history_manager.get_history(symbol, bars=200, include_forming=include_forming)
    if df is None or len(df) < 26:  # Need at least MACD slow period
        return None

    try:
        return get_indicator_cache().get_or_compute(df, symbol, timeframe=Timeframe.D1)
    except Exception as e:
        logger.warning(f"Indicator calc failed for {symbol}: {e}")
        return None


async def run_live_trading():
    """Run trading with live/simulated market data and real execution."""

    settings = get_settings()

    # Initialize dashboard
    data_source = "live" if is_market_open() else "simulated"
    dashboard = TradingDashboard()
    dashboard.start(balance=settings.paper_wallet_balance, mode=settings.trading_mode, data_source=data_source)

    # Setup tracing
    tracing_enabled = setup_tracing()
    dashboard.stats.log_activity(
        f"LangSmith: {'enabled' if tracing_enabled else 'disabled'}",
        "INFO"
    )

    # Create trading graph (now includes support agents)
    graph = create_trading_graph(include_support_agents=settings.enable_news_analysis)
    dashboard.stats.log_activity("Trading graph compiled (with support agents)", "SUCCESS")

    # Initialize memory & performance tracker
    memory_db = AgentMemoryDB()
    perf_tracker = get_performance_tracker()
    dashboard.stats.log_activity("Memory database + performance tracker ready", "INFO")

    # Learning feedback loop: classify closed trades into lessons and measure whether the
    # lessons that were injected actually helped. Optional (adds an LLM call per loss).
    mistake_classifier = MistakeClassifier() if settings.enable_learning else None
    memory_injector = MemoryInjector(memory_db=memory_db) if settings.enable_learning else None
    # Lesson IDs that were in the agents' context when each position was opened.
    active_lessons: dict[str, list[str]] = {}
    dashboard.stats.log_activity(
        f"Learning loop: {'enabled' if settings.enable_learning else 'disabled'}", "INFO"
    )

    # Initialize paper trading engine
    paper_engine = LocalPaperEngine(initial_balance=settings.paper_wallet_balance)
    dashboard.stats.log_activity(
        f"Paper engine: Rs. {paper_engine.get_balance():,.0f} balance", "INFO"
    )

    # Unified execution service: one mode-switched path for order submission with
    # idempotency (no double-submit on retry/restart) and shadow-mode safety.
    execution_service = ExecutionService.from_settings(
        engine=paper_engine,
        idempotency=IdempotencyStore(Path("paper_idempotency.json")),
    )
    effective = execution_service.effective_mode.value
    dashboard.stats.log_activity(
        f"Execution mode: {effective}"
        + (" (SHADOW — mirrors live, sends no real orders)" if effective == "shadow" else ""),
        "WARNING" if effective not in ("local_paper", "shadow") else "INFO",
    )

    # Live modes only: attach the broker executor and reconcile against the broker at
    # startup (broker = source of truth). Dormant by default — effective mode is shadow
    # unless allow_live_orders=True and Dhan creds are present.
    if effective in ("live", "dhan_paper"):
        try:
            from src.execution.adapter import ExecutionAdapter
            from src.execution.live_executor import LiveBrokerExecutor, reconcile_positions

            broker_adapter = ExecutionAdapter()
            execution_service.broker_executor = LiveBrokerExecutor(adapter=broker_adapter)
            broker_positions = await broker_adapter.get_positions()
            report = reconcile_positions(paper_engine.get_positions(), broker_positions)
            dashboard.stats.log_activity(
                f"Broker reconciliation: {report.summary()}",
                "INFO" if report.in_sync else "WARNING",
            )
            if not report.in_sync:
                await get_alert_manager().alert(
                    "position_drift", f"Startup reconciliation — {report.summary()}", level="WARNING"
                )
        except Exception as e:
            logger.warning("Live broker setup/reconciliation failed: %s", e)

    # Initialize exit manager
    exit_manager = ExitManager(
        trailing_atr_multiplier=1.5,
        breakeven_r_threshold=1.0,
        max_hold_minutes=240,
        partial_profit_r=1.0,
        partial_exit_pct=0.5,
    )
    dashboard.stats.log_activity("Exit manager initialized", "INFO")

    # Profit-target goal engine: derive a risk-bounded plan from the configured target.
    # Advisory only — it never changes position sizing or relaxes risk limits.
    goal_engine = ProfitGoalEngine()
    goal_plan = goal_engine.build_plan(paper_engine.get_balance())
    dashboard.stats.goal_enabled = goal_plan.enabled
    dashboard.stats.goal_feasible = goal_plan.feasible
    dashboard.stats.goal_target_amount = goal_plan.monthly_target_amount
    if goal_plan.enabled:
        wr = goal_plan.required_win_rate
        wr_str = "n/a" if wr == float("inf") else f"{wr:.0%}"
        dashboard.stats.log_activity(
            f"Profit goal: Rs.{goal_plan.monthly_target_amount:,.0f}/mo "
            f"(needs ~{wr_str} win rate @ {goal_plan.expected_trades_per_day}/day; "
            f"{'feasible' if goal_plan.feasible else 'NOT feasible within risk'})",
            "INFO" if goal_plan.feasible else "WARNING",
        )
        for warning in goal_plan.warnings:
            dashboard.stats.log_activity(f"Goal: {warning}", "WARNING")
    else:
        dashboard.stats.log_activity("Profit goal: disabled (no target configured)", "INFO")

    # Signal engine
    signal_engine = SignalEngine()

    # Dynamic Stock Discovery
    dashboard.stats.log_activity("Running dynamic stock discovery...", "INFO")
    discovery = StockDiscovery(max_stocks=15)
    trading_symbols = await discovery.discover()

    report = discovery.get_discovery_report()
    for item in report[:5]:
        dashboard.stats.log_activity(
            f"Discovered: {item['symbol']} ({item['source']}: {item['reason'][:30]}...)",
            "INFO"
        )

    # Initialize history manager and pre-fetch data
    dashboard.stats.log_activity("Pre-fetching historical data for real indicators...", "INFO")
    history_manager = HistoryManager(symbols=trading_symbols, lookback_period="3mo")
    fetch_results = history_manager.prefetch_all()
    loaded = sum(1 for v in fetch_results.values() if v)
    dashboard.stats.log_activity(
        f"Historical data loaded: {loaded}/{len(trading_symbols)} symbols", "SUCCESS"
    )

    # Market data manager
    market_manager = MarketDataManager(symbols=trading_symbols)

    console.print("\n[bold green]RakshaQuant Live Trading System Starting...[/]")
    market_mode = "LIVE" if is_market_open() else "SIMULATED"
    console.print(f"[dim]Mode: {market_mode} | Stocks: {len(trading_symbols)} | Press Ctrl+C to stop[/]\n")
    time.sleep(1)

    # Start market data
    is_live = await market_manager.start()
    data_source = "WebSocket LIVE" if is_live else "Simulated"
    dashboard.stats.log_activity(f"Data source: {data_source}", "SUCCESS")

    # Telegram startup notification (best-effort; no-op if unconfigured)
    notifier = get_notifier()
    if notifier.enabled:
        try:
            await notifier.send_startup_message()
            dashboard.stats.log_activity("Telegram notifications active", "INFO")
        except Exception as e:
            logger.warning("Telegram startup notification failed: %s", e)

    with Live(create_dashboard_layout(dashboard.stats), console=console, refresh_per_second=4) as live:

        async def _run_cycle(cycle: int) -> None:
            """
            Run a single trading cycle.

            Raises on any unexpected failure; the caller catches it so one bad
            cycle never tears down the whole loop. Early ``return`` is used for
            benign "nothing to do" outcomes (no candidates / signals / history).
            """
            # ── Step 0: Check exits on existing positions ───────────
            quotes = market_manager.get_all_quotes()
            market_prices = {s: q.last_price for s, q in quotes.items()}

            # Calculate ATR for exit manager
            atr_values = {}
            for symbol in market_prices:
                ind = calculate_real_indicators(history_manager, symbol)
                if ind and ind.atr:
                    atr_values[symbol] = ind.atr

            # Check all managed positions for exits
            current_regime = dashboard.stats.current_regime if hasattr(dashboard.stats, 'current_regime') else ""
            exit_signals = exit_manager.check_exits(market_prices, current_regime, atr_values)

            for pos, exit_rule in exit_signals:
                exit_price = market_prices.get(pos.symbol, pos.entry_price)
                # Execute exit via paper engine
                order = paper_engine.place_order(
                    symbol=pos.symbol, side="SELL" if pos.side == "BUY" else "BUY",
                    quantity=int(pos.quantity * exit_rule.partial_pct),
                    current_price=exit_price,
                )
                if order.status == "FILLED":
                    pnl = (exit_price - pos.entry_price) * pos.quantity * exit_rule.partial_pct
                    if pos.side != "BUY":
                        pnl = -pnl
                    dashboard.close_trade(pnl)
                    dashboard.stats.log_activity(
                        f"EXIT [{exit_rule.exit_type}]: {pos.symbol} @ Rs.{exit_price:,.2f} "
                        f"P&L: Rs.{pnl:+,.2f} — {exit_rule.reason}",
                        "TRADE"
                    )
                    # Record in performance tracker
                    perf_tracker.record_trade(
                        strategy=pos.strategy, regime=pos.regime_at_entry,
                        pnl=pnl, pnl_pct=(pnl / (pos.entry_price * pos.quantity)) * 100,
                        symbol=pos.symbol,
                    )
                    if exit_rule.partial_pct >= 1.0:
                        exit_manager.unregister_position(pos.position_id)
                        # ── Learning feedback (resilient; never disrupts trading) ──
                        # Turn the closed trade into a lesson, and mark the lessons that were
                        # active when this position was opened as successful/unsuccessful.
                        if settings.enable_learning and mistake_classifier and memory_injector:
                            pnl_pct = (
                                (pnl / (pos.entry_price * pos.quantity)) * 100
                                if pos.entry_price else 0.0
                            )
                            hold_minutes = int(
                                (datetime.now() - pos.entry_time).total_seconds() / 60
                            )
                            outcome = feedback.build_outcome(
                                trade_id=pos.position_id, symbol=pos.symbol,
                                strategy=pos.strategy, regime=pos.regime_at_entry,
                                side=pos.side, entry_price=pos.entry_price,
                                exit_price=exit_price, stop_loss=pos.stop_loss,
                                target_price=pos.target_price, pnl=pnl, pnl_pct=pnl_pct,
                                mae=pos.mae, mfe=pos.mfe, hold_minutes=hold_minutes,
                            )
                            mistake = feedback.learn_from_outcome(
                                memory_injector, mistake_classifier, outcome
                            )
                            if mistake:
                                dashboard.stats.log_activity(
                                    f"Lesson learned: [{mistake.severity}] {mistake.category}",
                                    "INFO",
                                )
                            feedback.mark_lessons_outcome(
                                memory_db, active_lessons.pop(pos.position_id, []),
                                was_successful=pnl > 0,
                            )
                    else:
                        pos.partial_taken = True

            live.update(create_dashboard_layout(dashboard.stats))

            # ── Step 1: Refresh market data ────────────────────────
            if not is_live:
                market_manager.refresh_simulated()

            quotes = market_manager.get_all_quotes()
            dashboard.update_market_data({s: q.to_dict() for s, q in quotes.items()})

            # Append new quotes to history for rolling indicator updates
            for symbol, quote in quotes.items():
                history_manager.append_quote(
                    symbol=symbol,
                    open_price=quote.open, high=quote.high,
                    low=quote.low, close=quote.last_price,
                    volume=quote.volume,
                )

            # ── Data freshness gate ────────────────────────────────
            # If the feed has stalled (even the freshest quote is too old), skip NEW
            # entries this cycle — trading on stale prices is unsafe. Exits in Step 0
            # already ran on the last known price.
            max_stale = settings.max_quote_staleness_seconds
            if max_stale > 0 and quotes:
                freshest_age = min(q.age_seconds for q in quotes.values())
                if freshest_age > max_stale:
                    await get_alert_manager().alert(
                        "data_staleness",
                        f"Market data stale: freshest quote is {freshest_age:.0f}s old "
                        f"(limit {max_stale}s). Skipping new entries this cycle.",
                        level="WARNING",
                    )
                    dashboard.stats.log_activity(
                        f"Stale data ({freshest_age:.0f}s old) — skipping trading this cycle",
                        "WARNING",
                    )
                    for _ in range(15):
                        time.sleep(1)
                        live.update(create_dashboard_layout(dashboard.stats))
                    return

            # ── Step 2: Find trading candidates ────────────────────
            candidates = market_manager.get_trading_candidates(min_change=0.3)

            if not candidates:
                dashboard.stats.log_activity("No trading candidates found", "INFO")
                live.update(create_dashboard_layout(dashboard.stats))
                for _ in range(15):
                    time.sleep(1)
                    live.update(create_dashboard_layout(dashboard.stats))
                return

            for c in candidates[:3]:
                direction = "UP" if c.is_bullish else "DOWN"
                dashboard.stats.log_activity(
                    f"Mover: {c.symbol} {c.change_percent:+.2f}% [{direction}]", "INFO"
                )
            live.update(create_dashboard_layout(dashboard.stats))

            # ── Step 3: Calculate REAL indicators ──────────────────
            top_candidate = candidates[0]
            indicators = calculate_real_indicators(history_manager, top_candidate.symbol)

            if indicators is None:
                dashboard.stats.log_activity(
                    f"Insufficient history for {top_candidate.symbol}", "WARNING"
                )
                for _ in range(10):
                    time.sleep(1)
                    live.update(create_dashboard_layout(dashboard.stats))
                return

            # ── Step 4: Generate signals from REAL indicators ──────
            signals = signal_engine.generate_signals(indicators)

            if signals:
                sig = signals[0]
                dashboard.set_current_signal(
                    sig.signal_type.value, sig.symbol,
                    sig.strategy.value, sig.confidence,
                )
                direction = "bullish" if top_candidate.is_bullish else "bearish"
                reason = (f"{direction.title()} momentum ({top_candidate.change_percent:+.2f}%) "
                         f"with {sig.strategy.value} strategy "
                         f"(RSI: {indicators.rsi:.1f}, ADX: {indicators.adx:.1f})")
                dashboard.set_decision_reason(reason)

            dashboard.stats.signals_generated += len(signals)
            for signal in signals:
                dashboard.stats.log_activity(
                    f"Signal: {signal.signal_type.value} {signal.symbol} "
                    f"[{signal.strategy.value}] conf={signal.confidence:.0%}",
                    "INFO"
                )
            live.update(create_dashboard_layout(dashboard.stats))

            if not signals:
                dashboard.stats.log_activity("No signals generated", "INFO")
                for _ in range(15):
                    time.sleep(1)
                    live.update(create_dashboard_layout(dashboard.stats))
                return

            # ── FinOps spend gate ──────────────────────────────────
            # The agent pipeline below is the only LLM spend. If the daily token/cost
            # budget is exhausted, skip it (and any new entries) to conserve spend —
            # exits in Step 0 already ran, so risk is still managed.
            cost_tracker = get_cost_tracker()
            if cost_tracker.is_over_hard_budget():
                status = cost_tracker.budget_status()
                await get_alert_manager().alert(
                    "finops_hard_budget",
                    f"Daily LLM budget exhausted ({status['tokens_used']:,} tokens / "
                    f"${status['cost_used_usd']:.4f} used) — pausing new agent cycles.",
                    level="CRITICAL",
                )
                dashboard.stats.log_activity(
                    "FinOps HARD budget reached — skipping agent pipeline this cycle", "WARNING"
                )
                for _ in range(15):
                    time.sleep(1)
                    live.update(create_dashboard_layout(dashboard.stats))
                return

            # ── Step 5: Run agent pipeline ─────────────────────────
            market_data = {s: q.to_dict() for s, q in quotes.items()}
            indicators_dict = {top_candidate.symbol: indicators.to_dict()}

            memory_lessons = memory_db.get_top_lessons_for_context(
                regime="trending_up" if top_candidate.is_bullish else "trending_down",
                strategies=["momentum", "trend_following"],
                n=5,
            )

            workflow_id = f"LIVE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{cycle}"

            daily_stats = {
                "trades_count": dashboard.stats.total_trades,
                "profit_loss": dashboard.stats.realized_pnl,
                "max_drawdown": 0,
            }
            portfolio = {
                "capital": paper_engine.get_balance(),
                "positions": [p.to_dict() for p in paper_engine.get_positions()],
            }

            final_state = await run_trading_cycle(
                graph=graph,
                market_data=market_data,
                indicators=indicators_dict,
                signals=[s.to_dict() for s in signals],
                memory_lessons=memory_lessons,
                portfolio=portfolio,
                daily_stats=daily_stats,
                thread_id=workflow_id,
            )

            # ── Step 6: Process results ────────────────────────────
            regime = final_state.get("regime", "unknown")
            confidence = final_state.get("regime_confidence", 0)
            strategies = final_state.get("active_strategies", [])
            dashboard.update_regime(regime, confidence, strategies)
            if hasattr(dashboard.stats, 'current_regime'):
                dashboard.stats.current_regime = regime
            live.update(create_dashboard_layout(dashboard.stats))

            validated = final_state.get("validated_signals", [])
            rejected = final_state.get("rejected_signals", [])
            dashboard.stats.signals_validated += len(validated)
            dashboard.stats.signals_rejected += len(rejected)

            for sig in validated:
                dashboard.stats.log_activity(
                    f"VALIDATED: {sig.get('signal_type')} {sig.get('symbol')}", "SUCCESS"
                )
            for sig in rejected:
                dashboard.stats.log_activity(
                    f"REJECTED: {sig.get('signal_type')} {sig.get('symbol')}", "WARNING"
                )
            live.update(create_dashboard_layout(dashboard.stats))

            # ── Step 7: Execute approved trades via paper engine ───
            approved = final_state.get("approved_trades", [])
            risk_rejected = final_state.get("risk_rejected", [])
            dashboard.stats.trades_approved += len(approved)
            dashboard.stats.trades_risk_rejected += len(risk_rejected)

            # Kill-switch gate: a daily-loss / drawdown breach must halt NEW entries
            # at the point of execution (exits in Step 0 still run, to flatten risk).
            # Previously the kill switch only ended the agent graph at the regime edge
            # and never re-checked here, so a mid-cycle breach still placed orders.
            if approved and check_kill_switch({"daily_stats": daily_stats, "portfolio": portfolio}):
                dashboard.stats.log_activity(
                    f"KILL SWITCH ACTIVE — blocking {len(approved)} approved "
                    f"{'entry' if len(approved) == 1 else 'entries'} (daily loss/drawdown limit)",
                    "WARNING",
                )
                approved = []

            for trade in approved:
                symbol = trade.get("symbol", "N/A")
                side = trade.get("signal_type", "BUY")
                entry_price = market_prices.get(symbol, trade.get("entry_price", 0))
                stop_loss = trade.get("stop_loss", entry_price * 0.98)
                target_price = trade.get("target_price", entry_price * 1.04)
                strategy = trade.get("strategy", "unknown")
                quantity = max(1, int((paper_engine.get_balance() * 0.05) / entry_price)) if entry_price > 0 else 1

                # Execute via the unified execution service (idempotent; shadow-aware;
                # awaits the broker for live modes).
                result = await execution_service.submit_async(
                    symbol=symbol, side=side, quantity=quantity, price=entry_price,
                    idempotency_key=f"{workflow_id}:{symbol}:{side}",
                )

                if result.is_duplicate:
                    dashboard.stats.log_activity(
                        f"DUPLICATE suppressed: {side} {symbol}", "INFO"
                    )
                    continue

                if result.filled:
                    tag = " [SHADOW]" if result.is_shadow else ""
                    dashboard.stats.log_activity(
                        f"TRADE{tag}: {side} {quantity} {symbol} @ Rs.{entry_price:,.2f}", "TRADE"
                    )
                    dashboard.add_position(symbol, side, quantity, entry_price)
                    dashboard.stats.current_balance = paper_engine.get_balance()

                    # Register with exit manager for tracking
                    exit_manager.register_position(
                        position_id=result.order_id,
                        symbol=symbol, side=side, quantity=quantity,
                        entry_price=entry_price, stop_loss=stop_loss,
                        target_price=target_price, strategy=strategy,
                        regime=regime,
                    )
                    # Remember which lessons were in the agents' context at entry, so we can
                    # mark them successful/unsuccessful when this position closes.
                    if settings.enable_learning:
                        active_lessons[result.order_id] = feedback.lesson_ids(memory_lessons)
                else:
                    dashboard.stats.log_activity(
                        f"ORDER {result.status}: {symbol} — {result.message}", "WARNING"
                    )

            live.update(create_dashboard_layout(dashboard.stats))

            # Update paper engine P&L with current prices
            paper_engine.update_positions_pnl(market_prices)

            # Update dashboard balance from paper engine
            dashboard.stats.current_balance = paper_engine.get_balance()

            # ── FinOps: surface today's LLM spend + soft-budget alert ──
            cost_summary = cost_tracker.daily_summary()
            dashboard.stats.llm_calls = cost_summary["calls"]
            dashboard.stats.llm_tokens = cost_summary["total_tokens"]
            dashboard.stats.llm_cost_usd = cost_summary["total_cost_usd"]
            budget = cost_tracker.budget_status()
            if budget["soft_breached"] and not budget["hard_breached"]:
                await get_alert_manager().alert(
                    "finops_soft_budget",
                    f"Approaching daily LLM budget: {budget['tokens_used']:,} tokens / "
                    f"${budget['cost_used_usd']:.4f} used "
                    f"(soft limit {budget['soft_pct']:.0%}).",
                    level="WARNING",
                )

            # ── Profit goal: pace tracking (advisory only — never changes sizing) ──
            goal_report = goal_engine.evaluate(
                capital=paper_engine.get_balance(),
                realized_pnl=dashboard.stats.realized_pnl,
            )
            if goal_report.get("enabled"):
                dashboard.stats.goal_mtd_pnl = goal_report["month_to_date_pnl"]
                dashboard.stats.goal_expected_to_date = goal_report["expected_to_date"]
                dashboard.stats.goal_on_pace = goal_report["on_pace"]
                dashboard.stats.goal_status = goal_report["status"]
                if not goal_report["feasible"]:
                    await get_alert_manager().alert(
                        "goal_infeasible",
                        "Profit target not feasible within risk budget. "
                        f"{goal_report['plan']['recommended_action']}",
                        level="WARNING",
                    )
                elif not goal_report["on_pace"]:
                    await get_alert_manager().alert(
                        "goal_off_pace",
                        f"Behind profit pace: month-to-date Rs.{goal_report['month_to_date_pnl']:,.0f} "
                        f"vs pace Rs.{goal_report['expected_to_date']:,.0f}. "
                        "Risk limits are fixed — do NOT increase position size to catch up.",
                        level="WARNING",
                    )

            # Increment cycle
            dashboard.increment_cycle()
            live.update(create_dashboard_layout(dashboard.stats))

            # Wait before next cycle
            wait_time = 20
            dashboard.stats.log_activity(f"Next cycle in {wait_time}s...", "INFO")
            live.update(create_dashboard_layout(dashboard.stats))

            for _ in range(wait_time):
                time.sleep(1)
                live.update(create_dashboard_layout(dashboard.stats))

        consecutive_errors = 0

        try:
            cycle = 0

            while True:
                cycle += 1
                dashboard.stats.log_activity(f"=== Trading Cycle #{cycle} ===", "INFO")
                live.update(create_dashboard_layout(dashboard.stats))

                try:
                    await _run_cycle(cycle)
                    consecutive_errors = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    # Per-cycle isolation: log, back off, and keep the loop alive.
                    consecutive_errors += 1
                    backoff = min(60, 5 * consecutive_errors)
                    logger.exception("Trading cycle #%d failed", cycle)
                    dashboard.stats.log_activity(
                        f"Cycle #{cycle} ERROR (failure #{consecutive_errors}): {e} "
                        f"— recovering in {backoff}s",
                        "ERROR",
                    )
                    if consecutive_errors >= 5:
                        dashboard.stats.log_activity(
                            "5+ consecutive cycle failures — check data source and logs",
                            "WARNING",
                        )
                    for _ in range(backoff):
                        time.sleep(1)
                        live.update(create_dashboard_layout(dashboard.stats))

        except KeyboardInterrupt:
            dashboard.stats.log_activity("Shutdown requested", "WARNING")
            live.update(create_dashboard_layout(dashboard.stats))

        finally:
            await market_manager.stop()
            # Show final stats
            stats = paper_engine.get_stats()
            cost_summary = get_cost_tracker().daily_summary()
            console.print(f"\n[yellow]Trading stopped[/]")
            console.print(f"[dim]Final balance: Rs.{stats['balance']:,.2f} | "
                         f"P&L: Rs.{stats['total_pnl']:+,.2f} | "
                         f"Win rate: {stats['win_rate']:.1f}%[/]")
            console.print(f"[dim]LLM spend today: {cost_summary['calls']} calls | "
                         f"{cost_summary['total_tokens']:,} tokens | "
                         f"${cost_summary['total_cost_usd']:.4f} (paid-tier equiv)[/]")

            # Telegram shutdown + P&L summary (best-effort)
            if notifier.enabled:
                try:
                    await notifier.send_pnl_summary(
                        balance=stats["balance"],
                        realized_pnl=stats["total_pnl"],
                        unrealized_pnl=0.0,
                        total_trades=stats.get("total_trades", 0),
                        win_rate=stats["win_rate"],
                    )
                    await notifier.send_shutdown_message(reason="Session ended")
                except Exception as e:
                    logger.warning("Telegram shutdown notification failed: %s", e)


def main():
    """Main entry point."""
    import atexit

    def suppress_threading_errors():
        import warnings
        warnings.filterwarnings("ignore", category=RuntimeWarning)

    atexit.register(suppress_threading_errors)

    try:
        asyncio.run(run_live_trading())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
