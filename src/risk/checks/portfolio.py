"""Portfolio-level checks (audit §L.3, minus PF_NET and VaR): positions, exposure, heat, cash,
MTM daily loss and drawdown. Every capacity check includes the batch's reservations."""

from __future__ import annotations

from decimal import Decimal

from src.domain.types import IntentKind, ReasonCode, RiskCheckResult
from src.risk.checks.base import OPENS, Check, block, f, resize, shares
from src.risk.snapshot import RiskContext

R = ReasonCode
_BUY_COST_BUFFER = Decimal("1.0065")  # price + 0.5% fill buffer + ~0.15% charges per rupee


def _new_symbol(ctx: RiskContext) -> bool:
    key = ctx.instrument.key
    held = ctx.position is not None and ctx.position.quantity != 0
    return not held and key not in ctx.reserved.symbols


def max_positions(ctx: RiskContext) -> RiskCheckResult | None:
    if not _new_symbol(ctx):
        return None
    held = sum(1 for p in ctx.snapshot.positions.values() if p.quantity)
    count = held + len(ctx.reserved.symbols)
    if count >= ctx.limits.max_positions:
        return block(R.PF_MAX_POSITIONS, "maximum open positions reached",
                     observed=count, limit=ctx.limits.max_positions)  # fmt: skip
    return None


def duplicate(ctx: RiskContext) -> RiskCheckResult | None:
    if ctx.intent.kind is IntentKind.OPEN and not _new_symbol(ctx):
        return block(R.PF_DUPLICATE, f"already holding (or buying) {ctx.instrument.symbol}")
    return None


def reentry_same_day(ctx: RiskContext) -> RiskCheckResult | None:
    key = ctx.instrument.key
    if key in ctx.snapshot.symbols_exited_today or key in ctx.snapshot.symbols_entered_today:
        return block(R.PF_REENTRY_SAME_DAY, f"{ctx.instrument.symbol} already traded today")
    return None


def gross(ctx: RiskContext) -> RiskCheckResult | None:
    price = ctx.price
    if price is None:
        return None
    limit = ctx.pct(ctx.limits.max_gross_exposure_pct)
    used = ctx.snapshot.gross_exposure + ctx.reserved.gross
    if used >= limit:
        return block(R.PF_GROSS, "gross exposure limit reached", observed=f(used), limit=f(limit))
    return resize(R.PF_GROSS, shares(limit - used, price), "gross exposure room",
                  observed=f(used), limit=f(limit))  # fmt: skip


def sector(ctx: RiskContext) -> RiskCheckResult | None:
    price = ctx.price
    if price is None:
        return None
    name = ctx.sector
    limit = ctx.pct(ctx.limits.max_sector_pct)
    used = ctx.snapshot.sector_exposure(name) + ctx.reserved.by_sector.get(name, Decimal(0))
    if used >= limit:
        return block(R.PF_SECTOR, f"sector {name} limit reached", observed=f(used), limit=f(limit))
    return resize(R.PF_SECTOR, shares(limit - used, price), f"sector {name} room",
                  observed=f(used), limit=f(limit))  # fmt: skip


def heat(ctx: RiskContext) -> RiskCheckResult | None:
    distance = ctx.stop_distance
    if distance is None or distance <= 0:
        return None
    limit = ctx.pct(ctx.limits.max_heat_pct)
    used = ctx.snapshot.heat + ctx.reserved.heat
    if used >= limit:
        return block(R.PF_HEAT, "portfolio heat limit reached", observed=f(used), limit=f(limit))
    return resize(R.PF_HEAT, shares(limit - used, distance), "portfolio heat room",
                  observed=f(used), limit=f(limit))  # fmt: skip


def cash(ctx: RiskContext) -> RiskCheckResult | None:
    price = ctx.price
    if price is None or ctx.intent.side.sign < 0:
        return None
    available = ctx.snapshot.cash - ctx.reserved.cash
    if available <= 0:
        return block(R.PF_CASH, "no free cash", observed=f(available))
    return resize(R.PF_CASH, shares(available, price * _BUY_COST_BUFFER), "cash available",
                  observed=f(available))  # fmt: skip


def daily_loss_mtm(ctx: RiskContext) -> RiskCheckResult | None:
    limit = ctx.snapshot.sod_equity * Decimal(str(ctx.limits.daily_loss_limit_pct))
    pnl = ctx.snapshot.day_pnl_mtm
    if pnl <= -limit:
        return block(R.PF_DAILY_LOSS_MTM, "daily loss limit reached (mark-to-market)",
                     observed=f(pnl), limit=f(-limit))  # fmt: skip
    return None


def drawdown(ctx: RiskContext) -> RiskCheckResult | None:
    peak = ctx.snapshot.peak_equity
    if peak <= 0:
        return None
    dd = (peak - ctx.snapshot.equity) / peak
    if dd >= Decimal(str(ctx.limits.max_drawdown_pct)):
        return block(R.PF_DRAWDOWN, "drawdown limit reached",
                     observed=f(dd), limit=ctx.limits.max_drawdown_pct)  # fmt: skip
    return None


def entries_per_day(ctx: RiskContext) -> RiskCheckResult | None:
    count = ctx.snapshot.entries_today + ctx.reserved.entries
    if count >= ctx.limits.max_entries_per_day:
        return block(R.PF_ENTRIES_PER_DAY, "maximum entries for the day reached",
                     observed=count, limit=ctx.limits.max_entries_per_day)  # fmt: skip
    return None


CHECKS = (
    Check(R.PF_MAX_POSITIONS, OPENS, max_positions),
    Check(R.PF_DUPLICATE, OPENS, duplicate),
    Check(R.PF_REENTRY_SAME_DAY, OPENS, reentry_same_day),
    Check(R.PF_GROSS, OPENS, gross),
    Check(R.PF_SECTOR, OPENS, sector),
    Check(R.PF_HEAT, OPENS, heat),
    Check(R.PF_CASH, OPENS, cash),
    Check(R.PF_DAILY_LOSS_MTM, OPENS, daily_loss_mtm),
    Check(R.PF_DRAWDOWN, OPENS, drawdown),
    Check(R.PF_ENTRIES_PER_DAY, OPENS, entries_per_day),
)
