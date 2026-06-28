"""Market data module for real-time data ingestion and processing.

Provides:
- Market data feeds (YFinance, simulated, live)
- Technical indicator calculations
- Signal generation engine
- Position sizing calculations
"""

from .data_feed import MarketDataFeed
from .indicators import calculate_indicators
from .signals import SignalEngine
from .sizing import (
    PositionSizer,
    PositionSizeResult,
    calculate_portfolio_heat,
    calculate_position_size,
)

__all__ = [
    "MarketDataFeed",
    "calculate_indicators",
    "SignalEngine",
    # Position sizing
    "PositionSizer",
    "PositionSizeResult",
    "calculate_position_size",
    "calculate_portfolio_heat",
]
