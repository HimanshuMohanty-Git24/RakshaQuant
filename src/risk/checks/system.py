"""System-level risk checks (audit §L.3). M4 wraps these in the RiskEngine's check protocol."""

from __future__ import annotations

from src.domain.types import (
    CheckLevel,
    CheckOutcome,
    MarketDataSource,
    ReasonCode,
    RiskCheckResult,
)

DEMO_ENVIRONMENT = "demo"


def check_data_source(source: MarketDataSource | None, environment: str) -> RiskCheckResult:
    """``SYS_DATA_SIMULATED`` (plan M2.6): simulated or synthetic prices may only ever trade in
    the ``demo`` environment, which has its own state directory. An unknown source blocks too.
    """
    if source is None:
        return RiskCheckResult(
            code=ReasonCode.SYS_DATA_SIMULATED,
            level=CheckLevel.SYSTEM,
            outcome=CheckOutcome.BLOCK,
            observed="unknown",
            message="market data source unknown: no orders",
        )
    if source.is_fabricated and environment != DEMO_ENVIRONMENT:
        return RiskCheckResult(
            code=ReasonCode.SYS_DATA_SIMULATED,
            level=CheckLevel.SYSTEM,
            outcome=CheckOutcome.BLOCK,
            observed=source.value,
            limit=f"only in environment '{DEMO_ENVIRONMENT}'",
            message=f"{source.value} prices cannot create orders in environment '{environment}'",
        )
    return RiskCheckResult(
        code=ReasonCode.SYS_DATA_SIMULATED,
        level=CheckLevel.SYSTEM,
        outcome=CheckOutcome.ALLOW,
        observed=source.value,
    )


def legacy_source(name: str) -> MarketDataSource | None:
    """The legacy MarketDataManager's ``data_source`` ("dhan"/"yfinance"/"simulated") as the
    canonical enum; anything else is unknown (and therefore blocks)."""
    try:
        return MarketDataSource(name)
    except ValueError:
        return None
