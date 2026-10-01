"""Order-level checks (audit §L.3): sizing caps, stop sanity, price sanity, shorting, reduce-only."""

from __future__ import annotations

from decimal import Decimal

from src.brokers.simulated.fill_model import on_tick
from src.domain.types import ReasonCode, RiskCheckResult
from src.risk.checks.base import ALL_KINDS, OPENS, REDUCES, Check, block, f, resize, shares
from src.risk.snapshot import RiskContext

R = ReasonCode


def qty_nonpos(ctx: RiskContext) -> RiskCheckResult | None:
    if ctx.requested_qty is not None and ctx.requested_qty <= 0:
        return block(R.ORD_QTY_NONPOS, "quantity must be positive", observed=ctx.requested_qty)
    return None


def notional_max(ctx: RiskContext) -> RiskCheckResult | None:
    price = ctx.price
    if price is None:
        return None  # SYS_DATA_STALE blocks opens without a fresh price
    cap = min(ctx.pct(ctx.limits.max_position_pct), Decimal(str(ctx.limits.max_position_inr)))
    return resize(
        R.ORD_NOTIONAL_MAX,
        shares(cap, price),
        f"position capped at Rs {cap:,.0f}",
        limit=f(cap),
    )


def risk_per_trade(ctx: RiskContext) -> RiskCheckResult | None:
    distance = ctx.stop_distance
    if ctx.intent.stop_price is None or distance is None:
        return block(R.ORD_RISK_PER_TRADE, "no stop: the risk of an entry cannot be sized")
    if distance <= 0:
        return None  # ORD_STOP_WRONG_SIDE reports it
    fraction = ctx.limits.risk_per_trade
    stats = ctx.snapshot.strategies.get(ctx.intent.strategy)
    if (
        ctx.limits.kelly_enabled
        and stats is not None
        and stats.trades >= ctx.limits.kelly_min_trades
        and stats.win_rate is not None
        and stats.payoff
    ):
        kelly = stats.win_rate - (1 - stats.win_rate) / stats.payoff
        fraction = min(fraction, max(0.0, kelly * ctx.limits.kelly_scale))
    budget = ctx.pct(fraction)
    return resize(
        R.ORD_RISK_PER_TRADE,
        shares(budget, distance),
        f"risk capped at {fraction:.2%} of equity (Rs {budget:,.0f}) over a {distance} stop",
        observed=f(distance),
        limit=f(budget),
    )


def stop_wrong_side(ctx: RiskContext) -> RiskCheckResult | None:
    distance = ctx.stop_distance
    if distance is not None and distance <= 0:
        return block(
            R.ORD_STOP_WRONG_SIDE,
            f"{ctx.intent.side} with stop {ctx.intent.stop_price} at/through price {ctx.price}",
            observed=f(ctx.intent.stop_price or 0),
            limit=f(ctx.price or 0),
        )
    return None


def stop_too_wide(ctx: RiskContext) -> RiskCheckResult | None:
    distance, price = ctx.stop_distance, ctx.price
    if distance is None or price is None or distance <= 0:
        return None
    if distance / price > Decimal(str(ctx.limits.max_stop_pct)):
        return block(R.ORD_STOP_TOO_WIDE, "stop too far from the price",
                     observed=f(distance / price), limit=ctx.limits.max_stop_pct)  # fmt: skip
    atr = ctx.facts.atr
    if atr is not None and atr > 0 and distance > atr * Decimal(str(ctx.limits.stop_atr_max)):
        return block(R.ORD_STOP_TOO_WIDE, "stop wider than the ATR limit",
                     observed=f(distance / atr), limit=ctx.limits.stop_atr_max)  # fmt: skip
    return None


def stop_too_tight(ctx: RiskContext) -> RiskCheckResult | None:
    distance, atr = ctx.stop_distance, ctx.facts.atr
    if distance is None or distance <= 0 or atr is None or atr <= 0:
        return None
    if distance < atr * Decimal(str(ctx.limits.stop_atr_min)):
        return block(R.ORD_STOP_TOO_TIGHT, "stop inside the noise",
                     observed=f(distance / atr), limit=ctx.limits.stop_atr_min)  # fmt: skip
    return None


def rr_min(ctx: RiskContext) -> RiskCheckResult | None:
    distance, price, target = ctx.stop_distance, ctx.price, ctx.intent.target_price
    if distance is None or price is None or target is None or distance <= 0:
        return None
    reward = (target - price) if ctx.intent.side.sign > 0 else (price - target)
    rr = reward / distance  # recomputed from the fresh price, not the signal's
    if rr < Decimal(str(ctx.limits.min_rr)):
        return block(R.ORD_RR_MIN, "reward:risk below the minimum at the current price",
                     observed=f(rr), limit=ctx.limits.min_rr)  # fmt: skip
    return None


def price_collar(ctx: RiskContext) -> RiskCheckResult | None:
    price = ctx.price
    if price is None:
        return None
    drift = abs(ctx.intent.decision_price / price - 1)
    if drift > Decimal(str(ctx.limits.price_collar_pct)):
        return block(R.ORD_PRICE_COLLAR, "decision price is far from the fresh price",
                     observed=f(drift), limit=ctx.limits.price_collar_pct)  # fmt: skip
    return None


def adv_pct(ctx: RiskContext) -> RiskCheckResult | None:
    adv = ctx.facts.adv_shares
    if adv is None or adv <= 0:
        return None
    cap = int(adv * ctx.limits.adv_pct)
    return resize(R.ORD_ADV_PCT, cap, f"order capped at {ctx.limits.adv_pct:.1%} of ADV",
                  observed=f(adv), limit=cap)  # fmt: skip


def circuit_band(ctx: RiskContext) -> RiskCheckResult | None:
    band, quote = ctx.instrument.band_pct, ctx.facts.quote
    if band is None or quote is None or not quote.prev_close:
        return None  # dynamic-band (F&O) stocks have no fixed circuit
    move = abs(quote.ltp / quote.prev_close - 1) * 100
    if move >= band - 0.5:
        return block(R.ORD_CIRCUIT_BAND, "at or near the circuit limit",
                     observed=round(move, 2), limit=band)  # fmt: skip
    return None


def tick(ctx: RiskContext) -> RiskCheckResult | None:
    tick_size = ctx.instrument.tick_size
    for label, price in (("limit", ctx.intent.limit_price), ("trigger", ctx.intent.trigger_price)):
        if price is not None and not on_tick(price, tick_size):
            return block(R.ORD_TICK, f"{label} {price} is off the {tick_size} tick")
    return None


def short_not_allowed(ctx: RiskContext) -> RiskCheckResult | None:
    if ctx.intent.side.sign < 0 and not ctx.limits.allow_short:
        return block(R.ORD_SHORT_NOT_ALLOWED, "opening a short is not allowed (CNC long-only)")
    return None


def reduce_exceeds_pos(ctx: RiskContext) -> RiskCheckResult | None:
    position = ctx.position
    held = 0 if position is None else position.quantity
    reducible = held if ctx.intent.side.sign < 0 else -held
    if reducible <= 0:
        return block(R.ORD_REDUCE_EXCEEDS_POS, "nothing to reduce", observed=held)
    return resize(R.ORD_REDUCE_EXCEEDS_POS, reducible, "a reduction never exceeds the position",
                  observed=held, limit=reducible)  # fmt: skip


CHECKS = (
    Check(R.ORD_QTY_NONPOS, ALL_KINDS, qty_nonpos),
    Check(R.ORD_NOTIONAL_MAX, OPENS, notional_max),
    Check(R.ORD_RISK_PER_TRADE, OPENS, risk_per_trade),
    Check(R.ORD_STOP_WRONG_SIDE, OPENS, stop_wrong_side),
    Check(R.ORD_STOP_TOO_WIDE, OPENS, stop_too_wide),
    Check(R.ORD_STOP_TOO_TIGHT, OPENS, stop_too_tight),
    Check(R.ORD_RR_MIN, OPENS, rr_min),
    Check(R.ORD_PRICE_COLLAR, OPENS, price_collar),
    Check(R.ORD_ADV_PCT, OPENS, adv_pct),
    Check(R.ORD_CIRCUIT_BAND, OPENS, circuit_band),
    Check(R.ORD_TICK, ALL_KINDS, tick),
    Check(R.ORD_SHORT_NOT_ALLOWED, OPENS, short_not_allowed),
    Check(R.ORD_REDUCE_EXCEEDS_POS, REDUCES, reduce_exceeds_pos),
)
