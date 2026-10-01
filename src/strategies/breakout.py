"""Breakout (ported from the legacy SignalEngine; shadow-only in month 1, plan D8)."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.domain.types import Side
from src.features.technical import Features
from src.strategies.base import Detection, StrategyParams, reason


@dataclass(frozen=True)
class Breakout:
    """A Bollinger squeeze (narrow bands) with the close outside a band."""

    params: StrategyParams = field(default_factory=StrategyParams)
    name: str = "breakout"
    rsi_contrarian: bool = False

    def detect(self, f: Features) -> Detection | None:
        width, upper, lower = f.bb_width, f.bb_upper, f.bb_lower
        if width is None or upper is None or lower is None or width >= self.params.bb_squeeze:
            return None
        squeeze = reason("bb_width", width, "squeeze")
        if f.close > upper:
            return Detection(Side.BUY, 0.7, (squeeze, reason("bb_upper", upper, "closed above")))
        if f.close < lower:
            return Detection(Side.SELL, 0.7, (squeeze, reason("bb_lower", lower, "closed below")))
        return None
