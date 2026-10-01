"""Trend following (ported from the legacy SignalEngine; shadow-only in month 1, plan D8)."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.domain.types import Side
from src.features.technical import Features
from src.strategies.base import Detection, StrategyParams, reason


@dataclass(frozen=True)
class TrendFollowing:
    """ADX above the trend threshold, DI in the direction, close beyond EMA 21."""

    params: StrategyParams = field(default_factory=StrategyParams)
    name: str = "trend_following"
    rsi_contrarian: bool = False

    def detect(self, f: Features) -> Detection | None:
        adx, plus, minus, ema21 = f.adx_14, f.plus_di_14, f.minus_di_14, f.ema.get(21)
        if adx is None or plus is None or minus is None or ema21 is None:
            return None
        p = self.params
        if adx <= p.adx_trend:
            return None
        base = 0.8 if adx > p.adx_strong else 0.6
        facts = (reason("adx_14", adx, "trending"), reason("plus_di_14", plus),
                 reason("minus_di_14", minus), reason("ema_21", ema21))  # fmt: skip
        if plus > minus and f.close > ema21:
            return Detection(Side.BUY, base, facts)
        if minus > plus and f.close < ema21:
            return Detection(Side.SELL, base, facts)
        return None
