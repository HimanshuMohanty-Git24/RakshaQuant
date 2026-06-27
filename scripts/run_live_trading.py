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
from src.market.manager import MarketDataManager, MarketQuote, is_market_open
from src.market.history_manager import HistoryManager
from src.market.indicators import calculate_indicators, Timeframe
from src.market.signals import SignalEngine
from src.market.stock_discovery import StockDiscovery
from src.memory.database import AgentMemoryDB
from src.memory.performance_tracker import get_performance_tracker
from src.execution.paper_engine import LocalPaperEngine
from src.execution.exit_manager import ExitManager
from src.observability.tracing import setup_tracing
from src.dashboard.cli import TradingDashboard, create_dashboard_layout

console = Console()
logger = logging.getLogger(__name__)


def calculate_real_indicators(history_manager: HistoryManager, symbol: str):
    """
    Calculate REAL indicators from historical data.

    This replaces the old quote_to_indicators() which fabricated
    all indicators from a single price point.
    """
    df = history_manager.get_history(symbol, bars=200)
    if df is None or len(df) < 26:  # Need at least MACD slow period
        return None

    try:
        return calculate_indicators(df, symbol, timeframe=Timeframe.D1)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Indicator calc failed for {symbol}: {e}")
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

    # Initialize paper trading engine
    paper_engine = LocalPaperEngine(initial_balance=settings.paper_wallet_balance)
    dashboard.stats.log_activity(
        f"Paper engine: Rs. {paper_engine.get_balance():,.0f} balance", "INFO"
    )

    # Initialize exit manager
    exit_manager = ExitManager(
        trailing_atr_multiplier=1.5,
        breakeven_r_threshold=1.0,
        max_hold_minutes=240,
        partial_profit_r=1.0,
        partial_exit_pct=0.5,
    )
    dashboard.stats.log_activity("Exit manager initialized", "INFO")

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

                # Execute via paper engine
                order = paper_engine.place_order(
                    symbol=symbol, side=side,
                    quantity=quantity, current_price=entry_price,
                )

                if order.status == "FILLED":
                    dashboard.stats.log_activity(
                        f"TRADE: {side} {quantity} {symbol} @ Rs.{entry_price:,.2f}", "TRADE"
                    )
                    dashboard.add_position(symbol, side, quantity, entry_price)
                    dashboard.stats.current_balance = paper_engine.get_balance()

                    # Register with exit manager for tracking
                    exit_manager.register_position(
                        position_id=order.order_id,
                        symbol=symbol, side=side, quantity=quantity,
                        entry_price=entry_price, stop_loss=stop_loss,
                        target_price=target_price, strategy=strategy,
                        regime=regime,
                    )
                else:
                    dashboard.stats.log_activity(
                        f"ORDER REJECTED: {symbol} — {order.status}", "WARNING"
                    )

            live.update(create_dashboard_layout(dashboard.stats))

            # Update paper engine P&L with current prices
            paper_engine.update_positions_pnl(market_prices)

            # Update dashboard balance from paper engine
            dashboard.stats.current_balance = paper_engine.get_balance()

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
            console.print(f"\n[yellow]Trading stopped[/]")
            console.print(f"[dim]Final balance: Rs.{stats['balance']:,.2f} | "
                         f"P&L: Rs.{stats['total_pnl']:+,.2f} | "
                         f"Win rate: {stats['win_rate']:.1f}%[/]")


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
