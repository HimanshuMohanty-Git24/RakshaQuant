"""System-level risk checks (audit §L.3). M4 wraps these in the RiskEngine's check protocol."""

from __future__ import annotations

from src.domain.types import (
    CheckLevel,
    CheckOutcome,
    KillSwitchState,
    MarketDataSource,
    ReasonCode,
    RiskCheckResult,
)
from src.risk.checks.base import ALL_KINDS, OPENS, Check, block, info
from src.risk.snapshot import RiskContext

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


# ---------------------------------------------------------------------------
# RiskEngine checks (plan M4.1)
# ---------------------------------------------------------------------------

R = ReasonCode


def _kill_global(ctx: RiskContext) -> RiskCheckResult | None:
    state = ctx.snapshot.kill.global_state
    if state is not KillSwitchState.ARMED:
        return block(R.SYS_KILL_GLOBAL, f"global kill switch is {state}")
    return None


def _kill_broker(ctx: RiskContext) -> RiskCheckResult | None:
    state = ctx.snapshot.kill.broker
    if state is not KillSwitchState.ARMED:
        return block(R.SYS_KILL_BROKER, f"broker kill switch is {state}")
    return None


def _session_closed(ctx: RiskContext) -> RiskCheckResult | None:
    snap = ctx.snapshot
    if snap.is_weekend or (snap.is_trading_day and not snap.session_open):
        return block(R.SYS_SESSION_CLOSED, "the market is closed")
    return None


def _holiday(ctx: RiskContext) -> RiskCheckResult | None:
    snap = ctx.snapshot
    if not snap.is_trading_day and not snap.is_weekend:
        return block(R.SYS_HOLIDAY, "exchange holiday")
    return None


def _entry_cutoff(ctx: RiskContext) -> RiskCheckResult | None:
    if not ctx.snapshot.entry_window:
        return block(R.SYS_ENTRY_CUTOFF, "outside the entry window")
    return None


def _data_stale(ctx: RiskContext) -> RiskCheckResult | None:
    quote = ctx.facts.quote
    if quote is None:
        return block(R.SYS_DATA_STALE, "no quote for the instrument")
    age = quote.age_seconds(ctx.snapshot.now)
    if age > ctx.limits.max_quote_age_s:
        return block(R.SYS_DATA_STALE, "quote too old", observed=round(age, 1),
                     limit=ctx.limits.max_quote_age_s)  # fmt: skip
    return None


def _data_simulated(ctx: RiskContext) -> RiskCheckResult | None:
    source = ctx.facts.data_source
    if source is None and not ctx.opening:
        return None  # an exit may go without our quote; only fabricated prices block it
    result = check_data_source(source, ctx.snapshot.environment)
    return result if result.outcome is CheckOutcome.BLOCK else None


def _max_open_orders(ctx: RiskContext) -> RiskCheckResult | None:
    count = ctx.snapshot.open_orders
    if count >= ctx.limits.max_open_orders:
        return block(R.SYS_MAX_OPEN_ORDERS, "too many open orders", observed=count,
                     limit=ctx.limits.max_open_orders)  # fmt: skip
    return None


def _order_rate(ctx: RiskContext) -> RiskCheckResult | None:
    rate = ctx.snapshot.orders_last_min + ctx.reserved.orders
    if rate >= ctx.limits.orders_per_min:
        return block(R.SYS_ORDER_RATE, "order rate limit", observed=rate,
                     limit=ctx.limits.orders_per_min)  # fmt: skip
    return None


def _reject_storm(ctx: RiskContext) -> RiskCheckResult | None:
    count = ctx.snapshot.rejects_in_window
    if count >= ctx.limits.reject_storm_count:
        return block(R.SYS_REJECT_STORM, "too many recent rejects", observed=count,
                     limit=ctx.limits.reject_storm_count)  # fmt: skip
    return None


def _unknown_order(ctx: RiskContext) -> RiskCheckResult | None:
    if ctx.instrument.key in ctx.snapshot.unknown_order_keys:
        return block(R.SYS_UNKNOWN_ORDER, "an order in this instrument has an UNKNOWN outcome")
    return None


def _llm_degraded(ctx: RiskContext) -> RiskCheckResult | None:
    if ctx.snapshot.llm_degraded:
        return info(R.SYS_LLM_DEGRADED, "AI advisor unavailable: the deterministic decision stands")
    return None


def _journal_not_durable(ctx: RiskContext) -> RiskCheckResult | None:
    if not ctx.snapshot.journal_durable:
        return block(R.SYS_JOURNAL_NOT_DURABLE, "the event store is not durable")
    return None


def _recon_drift(ctx: RiskContext) -> RiskCheckResult | None:
    if ctx.snapshot.recon_drift:
        return block(R.SYS_RECON_DRIFT, "the book disagrees with the broker")
    return None


CHECKS = (
    Check(R.SYS_SESSION_CLOSED, ALL_KINDS, _session_closed),
    Check(R.SYS_HOLIDAY, ALL_KINDS, _holiday),
    Check(R.SYS_DATA_SIMULATED, ALL_KINDS, _data_simulated),
    Check(R.SYS_KILL_GLOBAL, OPENS, _kill_global),
    Check(R.SYS_KILL_BROKER, OPENS, _kill_broker),
    Check(R.SYS_ENTRY_CUTOFF, OPENS, _entry_cutoff),
    Check(R.SYS_DATA_STALE, OPENS, _data_stale),
    Check(R.SYS_MAX_OPEN_ORDERS, OPENS, _max_open_orders),
    Check(R.SYS_ORDER_RATE, OPENS, _order_rate),
    Check(R.SYS_REJECT_STORM, OPENS, _reject_storm),
    Check(R.SYS_UNKNOWN_ORDER, OPENS, _unknown_order),
    Check(R.SYS_LLM_DEGRADED, OPENS, _llm_degraded),
    Check(R.SYS_JOURNAL_NOT_DURABLE, OPENS, _journal_not_durable),
    Check(R.SYS_RECON_DRIFT, OPENS, _recon_drift),
)
