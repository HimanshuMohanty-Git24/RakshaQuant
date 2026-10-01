"""Strategy-level checks (audit §L.3): enablement, kill switch, capital, losses, order rate."""

from __future__ import annotations

from decimal import Decimal

from src.domain.types import KillSwitchState, ReasonCode, RiskCheckResult
from src.risk.checks.base import OPENS, Check, block, f, resize, shares
from src.risk.snapshot import RiskContext, StrategyStats

R = ReasonCode


def _stats(ctx: RiskContext) -> StrategyStats:
    return ctx.snapshot.strategies.get(ctx.intent.strategy, StrategyStats())


def halted(ctx: RiskContext) -> RiskCheckResult | None:
    state = ctx.snapshot.kill.strategies.get(ctx.intent.strategy, KillSwitchState.ARMED)
    if state is not KillSwitchState.ARMED:
        return block(R.STR_HALTED, f"strategy {ctx.intent.strategy} kill switch is {state}")
    return None


def not_validated(ctx: RiskContext) -> RiskCheckResult | None:
    if ctx.intent.strategy not in ctx.limits.enabled_strategies:
        return block(
            R.STR_NOT_VALIDATED,
            f"{ctx.intent.strategy} is not enabled for trading (shadow only)",
            limit=",".join(ctx.limits.enabled_strategies),
        )
    return None


def capital_alloc(ctx: RiskContext) -> RiskCheckResult | None:
    price = ctx.price
    if price is None:
        return None
    strategy = ctx.intent.strategy
    budget = ctx.pct(ctx.limits.strategy_capital_pct)
    used = ctx.snapshot.deployed(strategy) + ctx.reserved.by_strategy.get(strategy, Decimal(0))
    room = budget - used
    if room <= 0:
        return block(R.STR_CAPITAL_ALLOC, f"{strategy} has used its allocation",
                     observed=f(used), limit=f(budget))  # fmt: skip
    return resize(R.STR_CAPITAL_ALLOC, shares(room, price), f"{strategy} allocation room",
                  observed=f(used), limit=f(budget))  # fmt: skip


def daily_loss(ctx: RiskContext) -> RiskCheckResult | None:
    limit = ctx.snapshot.sod_equity * Decimal(str(ctx.limits.strategy_daily_loss_pct))
    pnl = _stats(ctx).day_pnl
    if pnl <= -limit:
        return block(R.STR_DAILY_LOSS, f"{ctx.intent.strategy} hit its daily loss limit",
                     observed=f(pnl), limit=f(-limit))  # fmt: skip
    return None


def consec_losses(ctx: RiskContext) -> RiskCheckResult | None:
    streak = _stats(ctx).consecutive_losses
    if streak >= ctx.limits.strategy_max_consec_losses:
        return block(R.STR_CONSEC_LOSSES, f"{ctx.intent.strategy} lost {streak} in a row",
                     observed=streak, limit=ctx.limits.strategy_max_consec_losses)  # fmt: skip
    return None


def order_rate(ctx: RiskContext) -> RiskCheckResult | None:
    rate = _stats(ctx).orders_last_min
    if rate >= ctx.limits.strategy_orders_per_min:
        return block(R.STR_ORDER_RATE, f"{ctx.intent.strategy} order rate too high",
                     observed=rate, limit=ctx.limits.strategy_orders_per_min)  # fmt: skip
    return None


CHECKS = (
    Check(R.STR_HALTED, OPENS, halted),
    Check(R.STR_NOT_VALIDATED, OPENS, not_validated),
    Check(R.STR_CAPITAL_ALLOC, OPENS, capital_alloc),
    Check(R.STR_DAILY_LOSS, OPENS, daily_loss),
    Check(R.STR_CONSEC_LOSSES, OPENS, consec_losses),
    Check(R.STR_ORDER_RATE, OPENS, order_rate),
)
