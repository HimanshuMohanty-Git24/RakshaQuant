"""
RakshaQuant Trading with Live Dashboard

Runs the agentic trading system with a real-time CLI dashboard.
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.live import Live

from src.agents.graph import create_trading_graph, run_trading_cycle
from src.config import get_settings
from src.dashboard.cli import TradingDashboard, create_dashboard_layout
from src.market.indicators import IndicatorResult, Timeframe
from src.market.signals import SignalEngine
from src.memory.database import AgentMemoryDB
from src.observability.tracing import setup_tracing

console = Console()


def create_sample_indicators() -> IndicatorResult:
    """Create sample indicator data for demo."""
    return IndicatorResult(
        symbol="RELIANCE",
        timeframe=Timeframe.M5,
        open=2480.0,
        high=2510.0,
        low=2475.0,
        close=2500.0,
        volume=1000000,
        sma={20: 2450.0, 50: 2400.0, 200: 2300.0},
        ema={9: 2490.0, 21: 2470.0, 55: 2430.0},
        rsi=55.0,
        stoch_k=65.0,
        stoch_d=60.0,
        macd=15.0,
        macd_signal=10.0,
        macd_histogram=5.0,
        adx=30.0,
        plus_di=28.0,
        minus_di=18.0,
        atr=25.0,
        bb_upper=2530.0,
        bb_middle=2480.0,
        bb_lower=2430.0,
        bb_percent=0.7,
        vwap=2485.0,
    )


async def run_trading_with_dashboard():
    """Run trading with live dashboard updates."""

    settings = get_settings()

    # Initialize dashboard
    dashboard = TradingDashboard()
    dashboard.start(balance=1000000.0, mode=settings.trading_mode)

    # Setup tracing
    tracing_enabled = setup_tracing()
    dashboard.stats.log_activity(
        f"LangSmith: {'enabled' if tracing_enabled else 'disabled'}", "INFO"
    )

    # Create trading graph
    graph = create_trading_graph()
    dashboard.stats.log_activity("Trading graph compiled", "SUCCESS")

    # Initialize memory
    memory_db = AgentMemoryDB()
    dashboard.stats.log_activity("Memory database ready", "INFO")

    # Signal engine
    signal_engine = SignalEngine()

    console.print("\n[bold green]RakshaQuant Trading System Starting...[/]")
    console.print("[dim]Press Ctrl+C to stop[/]\n")
    time.sleep(1)

    with Live(
        create_dashboard_layout(dashboard.stats), console=console, refresh_per_second=4
    ) as live:
        try:
            cycle = 0

            while True:
                cycle += 1
                dashboard.stats.log_activity(f"Starting trading cycle #{cycle}", "INFO")
                live.update(create_dashboard_layout(dashboard.stats))

                # Generate sample data (replace with live data in production)
                sample_indicators = create_sample_indicators()
                signals = signal_engine.generate_signals(sample_indicators)

                dashboard.stats.signals_generated += len(signals)

                for signal in signals:
                    dashboard.stats.log_activity(
                        f"Signal: {signal.signal_type.value} {signal.symbol} [{signal.strategy.value}]",
                        "INFO",
                    )

                live.update(create_dashboard_layout(dashboard.stats))

                # Get memory lessons
                memory_lessons = memory_db.get_top_lessons_for_context(
                    regime="trending_up",
                    strategies=["momentum", "trend_following"],
                    n=5,
                )

                # Run trading cycle
                workflow_id = f"CYCLE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{cycle}"

                final_state = await run_trading_cycle(
                    graph=graph,
                    market_data={"RELIANCE": {"close": 2500.0, "change_percent": 1.5}},
                    indicators={"RELIANCE": sample_indicators.to_dict()},
                    signals=[s.to_dict() for s in signals],
                    memory_lessons=memory_lessons,
                    portfolio={"capital": dashboard.stats.current_balance, "positions": []},
                    daily_stats={
                        "trades_count": dashboard.stats.total_trades,
                        "profit_loss": dashboard.stats.realized_pnl,
                        "max_drawdown": 0,
                    },
                    thread_id=workflow_id,
                )

                # Update dashboard with results
                regime = final_state.get("regime", "unknown")
                confidence = final_state.get("regime_confidence", 0)
                strategies = final_state.get("active_strategies", [])

                dashboard.update_regime(regime, confidence, strategies)
                live.update(create_dashboard_layout(dashboard.stats))

                # Log validated/rejected signals
                validated = final_state.get("validated_signals", [])
                rejected = final_state.get("rejected_signals", [])

                dashboard.stats.signals_validated += len(validated)
                dashboard.stats.signals_rejected += len(rejected)

                for sig in validated:
                    dashboard.stats.log_activity(
                        f"VALIDATED: {sig.get('signal_type', 'N/A')} {sig.get('symbol', 'N/A')}",
                        "SUCCESS",
                    )

                for sig in rejected:
                    dashboard.stats.log_activity(
                        f"REJECTED: {sig.get('signal_type', 'N/A')} {sig.get('symbol', 'N/A')}",
                        "WARNING",
                    )

                live.update(create_dashboard_layout(dashboard.stats))

                # Log approved/risk-rejected trades
                approved = final_state.get("approved_trades", [])
                risk_rejected = final_state.get("risk_rejected", [])

                dashboard.stats.trades_approved += len(approved)
                dashboard.stats.trades_risk_rejected += len(risk_rejected)

                for trade in approved:
                    symbol = trade.get("symbol", "N/A")
                    side = trade.get("signal_type", "N/A")
                    price = trade.get("entry_price", 0)

                    dashboard.stats.log_activity(
                        f"TRADE APPROVED: {side} {symbol} @ Rs. {price:,.2f}", "TRADE"
                    )

                    # Simulate position
                    dashboard.add_position(symbol, side, 10, price)

                live.update(create_dashboard_layout(dashboard.stats))

                # Increment cycle
                dashboard.increment_cycle()
                live.update(create_dashboard_layout(dashboard.stats))

                # Simulate trade closure (for demo)
                if approved and cycle % 3 == 0:
                    import random

                    pnl = random.uniform(-500, 1500)
                    dashboard.close_trade(pnl)
                    dashboard.stats.open_positions = []
                    live.update(create_dashboard_layout(dashboard.stats))

                # Wait before next cycle
                dashboard.stats.log_activity("Waiting for next cycle (30s)...", "INFO")
                live.update(create_dashboard_layout(dashboard.stats))

                for _ in range(30):  # 30 second wait with updates
                    time.sleep(1)
                    live.update(create_dashboard_layout(dashboard.stats))

        except KeyboardInterrupt:
            dashboard.stats.log_activity("Shutdown requested", "WARNING")
            live.update(create_dashboard_layout(dashboard.stats))
            console.print("\n[yellow]Trading stopped by user[/]")


def main():
    """Main entry point."""
    try:
        asyncio.run(run_trading_with_dashboard())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise


if __name__ == "__main__":
    main()
