"""
Run Trading Script

Main entry point for the trading agent system.
Orchestrates the complete trading workflow.
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agents.graph import create_trading_graph, run_trading_cycle
from src.config import get_settings
from src.market.indicators import IndicatorResult, Timeframe
from src.market.signals import SignalEngine
from src.memory.database import AgentMemoryDB
from src.memory.injection import MemoryInjector
from src.observability.tracing import setup_tracing, trading_trace

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/trading_{datetime.now().strftime('%Y%m%d')}.log"),
    ],
)

logger = logging.getLogger(__name__)


async def run_demo_cycle():
    """
    Run a demonstration trading cycle with sample data.

    This demonstrates the full system flow without live market data.
    """
    logger.info("=" * 60)
    logger.info("AGENTIC PAPER TRADING SYSTEM - DEMO MODE")
    logger.info("=" * 60)

    # Setup tracing
    tracing_enabled = setup_tracing()
    logger.info(f"LangSmith tracing: {'enabled' if tracing_enabled else 'disabled'}")

    # Initialize components
    settings = get_settings()
    logger.info(f"Trading mode: {settings.trading_mode}")

    # Create the trading graph
    graph = create_trading_graph()
    logger.info("Trading graph compiled")

    # Initialize memory system
    memory_db = AgentMemoryDB()
    MemoryInjector(memory_db=memory_db)

    # Generate sample data
    logger.info("\n--- Generating Sample Market Data ---")
    sample_indicators = _create_sample_indicators()

    # Generate signals using signal engine
    signal_engine = SignalEngine()
    signals = signal_engine.generate_signals(sample_indicators)
    logger.info(f"Generated {len(signals)} signals")

    for signal in signals:
        logger.info(
            f"  Signal: {signal.signal_type.value} {signal.symbol} "
            f"[{signal.strategy.value}] confidence={signal.confidence:.2f}"
        )

    # Get relevant memory lessons
    memory_lessons = memory_db.get_top_lessons_for_context(
        regime="trending_up",
        strategies=["momentum", "trend_following"],
        n=5,
    )
    logger.info(f"Injected {len(memory_lessons)} memory lessons")

    # Run trading cycle
    logger.info("\n--- Running Trading Cycle ---")

    workflow_id = f"DEMO-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    with trading_trace(
        workflow_id=workflow_id,
        regime="trending_up",
        strategies=["momentum"],
        signals_count=len(signals),
    ):
        final_state = await run_trading_cycle(
            graph=graph,
            market_data={"RELIANCE": {"close": 2500.0, "change_percent": 1.5}},
            indicators={"RELIANCE": sample_indicators.to_dict()},
            signals=[s.to_dict() for s in signals],
            memory_lessons=memory_lessons,
            portfolio={"capital": 1000000, "positions": []},
            daily_stats={"trades_count": 0, "profit_loss": 0, "max_drawdown": 0},
            thread_id=workflow_id,
        )

    # Print results
    logger.info("\n--- Trading Cycle Results ---")
    logger.info(
        f"Regime: {final_state.get('regime')} (confidence: {final_state.get('regime_confidence', 0):.2f})"
    )
    logger.info(f"Active strategies: {final_state.get('active_strategies', [])}")
    logger.info(f"Validated signals: {len(final_state.get('validated_signals', []))}")
    logger.info(f"Rejected signals: {len(final_state.get('rejected_signals', []))}")
    logger.info(f"Approved trades: {len(final_state.get('approved_trades', []))}")
    logger.info(f"Risk rejected: {len(final_state.get('risk_rejected', []))}")

    if final_state.get("risk_warnings"):
        logger.info(f"Warnings: {final_state['risk_warnings']}")

    # Execute approved trades (paper mode)
    trades_to_execute = final_state.get("trades_to_execute", [])

    if trades_to_execute:
        logger.info(f"\n--- Executing {len(trades_to_execute)} Trades ---")

        # Note: In demo mode, we skip actual execution
        logger.info("(Demo mode - skipping actual order placement)")

        for trade in trades_to_execute:
            logger.info(
                f"  Would execute: {trade.get('signal_type')} {trade.get('symbol')} "
                f"@ {trade.get('entry_price', 0):.2f}"
            )
    else:
        logger.info("\nNo trades approved for execution")

    # Print errors if any
    if final_state.get("errors"):
        logger.error(f"Errors: {final_state['errors']}")

    logger.info("\n" + "=" * 60)
    logger.info("DEMO CYCLE COMPLETE")
    logger.info("=" * 60)

    return final_state


def _create_sample_indicators() -> IndicatorResult:
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


async def main():
    """Main entry point."""
    try:
        await run_demo_cycle()
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
