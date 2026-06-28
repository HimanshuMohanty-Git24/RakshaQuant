"""
Trading cost model for the paper engine.

Models adverse slippage and NSE-style charges so paper P&L reflects what a live account
would actually keep (net of costs) rather than the unrealistically optimistic
fill-at-screen-price, zero-fee result. Values are configurable approximations of a typical
discount-broker NSE equity cost stack — they are intentionally simple and overridable, not a
regulatory-grade calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _num(value: Any, default: float) -> float:
    """
    Coerce to float, falling back to default for non-numeric values.

    Type-checked rather than relying on float() to raise, because a MagicMock (used as
    settings in some tests) is float()-able (returns 1.0) and would otherwise inject
    phantom costs.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


@dataclass(frozen=True)
class CostModel:
    """Slippage + charges applied to paper fills (all rates in basis points unless noted)."""

    slippage_bps: float = 0.0      # adverse price move on fill (e.g. 2 = 0.02%)
    brokerage_bps: float = 0.0     # broker commission as bps of notional
    brokerage_max: float = 0.0     # per-order brokerage cap in INR (0 = uncapped)
    statutory_bps: float = 0.0     # combined STT + exchange txn + SEBI + stamp, as bps
    gst_pct: float = 0.0           # GST as a percentage of brokerage

    @classmethod
    def zero(cls) -> CostModel:
        """A no-cost model (ideal fills) — used for pure-mechanics tests."""
        return cls()

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> CostModel:
        if settings is None:
            from src.config import get_settings

            settings = get_settings()
        return cls(
            slippage_bps=_num(getattr(settings, "paper_slippage_bps", 0.0), 0.0),
            brokerage_bps=_num(getattr(settings, "paper_brokerage_bps", 0.0), 0.0),
            brokerage_max=_num(getattr(settings, "paper_brokerage_max", 0.0), 0.0),
            statutory_bps=_num(getattr(settings, "paper_statutory_bps", 0.0), 0.0),
            gst_pct=_num(getattr(settings, "paper_gst_pct", 0.0), 0.0),
        )

    def fill_price(self, price: float, side: str) -> float:
        """Apply adverse slippage: a BUY fills a touch higher, a SELL a touch lower."""
        adjustment = price * self.slippage_bps / 10_000
        return price + adjustment if side.upper() == "BUY" else price - adjustment

    def charges(self, notional: float, side: str) -> float:
        """Total charges for one leg (entry or exit) on the given notional."""
        notional = abs(notional)
        brokerage = notional * self.brokerage_bps / 10_000
        if self.brokerage_max > 0:
            brokerage = min(brokerage, self.brokerage_max)
        statutory = notional * self.statutory_bps / 10_000
        gst = brokerage * self.gst_pct / 100
        return brokerage + statutory + gst
