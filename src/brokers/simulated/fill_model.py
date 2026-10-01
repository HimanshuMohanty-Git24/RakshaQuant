"""
The simulated fill model (plan M3.3; audit §M.2). Pure functions; the broker applies them.

* **Latency:** an order becomes eligible ``latency`` after submission (lognormal, median
  ~250 ms) and fills against the first quote *received* after that, never the decision snapshot.
  Eligibility uses the quote's ``receipt_ts``: YFinance's ``exchange_ts`` lags ~15 min, so it
  would never be after the submit time.
* **Spread:** a half-spread tiered by 20-day average daily turnover (ADV), at least half a tick.
* **Impact:** ``k * sigma_daily * sqrt(Q / ADV_shares)`` (square-root law, k ~ 0.7).
* **Rounding:** prices are rounded *adversely* to the tick (a buy rounds up, a sell down).
* **Determinism:** a seeded RNG per (``client_order_id``, date) makes paper runs replayable.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from src.domain.types import Side

_CRORE = 10_000_000.0


@dataclass(frozen=True)
class FillModelConfig:
    latency_median_ms: float = 250.0
    latency_sigma: float = 0.5
    # (minimum ADV turnover in INR, half-spread in bps), most liquid first (audit §M.2).
    spread_tiers: tuple[tuple[float, float], ...] = (
        (500 * _CRORE, 1.0),
        (50 * _CRORE, 3.0),
        (5 * _CRORE, 8.0),
        (0.0, 20.0),
    )
    impact_k: float = 0.7
    participation: float = 0.10  # max share of the volume traded since the previous quote
    max_fill_quotes: int = 5  # a MARKET order that is still not filled after this many quotes
    default_sigma_daily: float = 0.02

    def __post_init__(self) -> None:
        if not 0 < self.participation <= 1:
            raise ValueError("participation must be in (0, 1]")
        if self.max_fill_quotes < 1:
            raise ValueError("max_fill_quotes must be >= 1")


@dataclass(frozen=True)
class MarketContext:
    """Per-instrument liquidity facts from the daily history (None = unknown)."""

    adv_inr: float | None = None  # 20-day average daily turnover
    adv_shares: float | None = None  # 20-day average daily volume
    sigma_daily: float | None = None  # daily return volatility (fraction)


def half_spread_bps(config: FillModelConfig, adv_inr: float | None) -> float:
    if adv_inr is None:
        return config.spread_tiers[-1][1]  # unknown liquidity: assume the widest
    for floor, bps in config.spread_tiers:
        if adv_inr >= floor:
            return bps
    return config.spread_tiers[-1][1]


def impact_fraction(config: FillModelConfig, quantity: int, context: MarketContext) -> float:
    if not context.adv_shares or context.adv_shares <= 0:
        return 0.0
    sigma = context.sigma_daily if context.sigma_daily else config.default_sigma_daily
    return config.impact_k * sigma * math.sqrt(quantity / context.adv_shares)


def round_to_tick(price: Decimal, tick: Decimal, side: Side) -> Decimal:
    """Round adversely to the tick: up for a buy, down for a sell."""
    steps = price / tick
    whole = steps.to_integral_value(rounding=ROUND_CEILING if side is Side.BUY else ROUND_FLOOR)
    return whole * tick


def on_tick(price: Decimal, tick: Decimal) -> bool:
    return (price / tick) == (price / tick).to_integral_value()


def adverse_price(
    reference: Decimal,
    side: Side,
    *,
    tick: Decimal,
    config: FillModelConfig,
    context: MarketContext,
    quantity: int,
) -> Decimal:
    """The fill price: reference moved against us by half-spread + impact, rounded to the tick."""
    cost = (half_spread_bps(config, context.adv_inr) / 10_000) + impact_fraction(
        config, quantity, context
    )
    move = max(reference * Decimal(str(cost)), tick / 2)
    raw = reference + move if side is Side.BUY else reference - move
    price = round_to_tick(raw, tick, side)
    return max(price, tick)


def rng_for(client_order_id: str, day: date) -> random.Random:
    seed = hashlib.sha256(f"{client_order_id}:{day.isoformat()}".encode()).hexdigest()[:16]
    return random.Random(int(seed, 16))


def sample_latency_ms(rng: random.Random, config: FillModelConfig) -> float:
    return rng.lognormvariate(math.log(config.latency_median_ms), config.latency_sigma)
