"""Momentum (ported from the legacy SignalEngine): MACD histogram with RSI room to run."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.domain.types import Side
from src.features.technical import Features
from src.strategies.base import Detection, StrategyParams, reason


@dataclass(frozen=True)
class Momentum:
    """BUY: RSI below 50 with a positive MACD histogram. SELL: the mirror image."""

    params: StrategyParams = field(default_factory=StrategyParams)
    name: str = "momentum"
    rsi_contrarian: bool = False

    def detect(self, f: Features) -> Detection | None:
        rsi, hist = f.rsi_14, f.macd_hist
        if rsi is None or hist is None:
            return None
        p = self.params
        if rsi < 50 and hist > 0:
            base = 0.8 if rsi < p.rsi_oversold else 0.6 if rsi < 40 else 0.4
            return Detection(Side.BUY, base, (
                reason("rsi_14", rsi, "below 50: room to run"),
                reason("macd_hist", hist, "positive: bullish momentum"),
            ))  # fmt: skip
        if rsi > 50 and hist < 0:
            base = 0.8 if rsi > p.rsi_overbought else 0.6 if rsi > 60 else 0.4
            return Detection(Side.SELL, base, (
                reason("rsi_14", rsi, "above 50: weakening"),
                reason("macd_hist", hist, "negative: bearish momentum"),
            ))  # fmt: skip
        return None
