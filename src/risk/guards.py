"""
Deterministic risk guards — independent of the LLM agent stack.

- DrawdownTracker: tracks intraday peak-to-trough equity drawdown **including unrealized P&L**.
  The live loop previously fed a hardcoded ``max_drawdown=0`` into the kill switch, so the
  drawdown limb never fired — you could be deep in unrealized loss while the system kept
  opening positions. This makes the drawdown real.
- is_circuit_locked: detects when a scrip is at/through its NSE price band (2/5/10/20%). Near a
  circuit you cannot get a sane fill (limit-up has no sellers; limit-down has no buyers), so the
  loop must not open new positions in locked names, and exits into a locked band may not fill.
"""

from __future__ import annotations

from dataclasses import dataclass

# Common NSE equity price band. Individual scrips sit in 2/5/10/20% bands; configurable.
NSE_DEFAULT_CIRCUIT_BAND_PCT = 10.0


@dataclass
class DrawdownTracker:
    """Tracks peak-to-trough equity drawdown over a session (rupees + percent)."""

    peak_equity: float = 0.0
    current_equity: float = 0.0
    max_drawdown: float = 0.0  # rupees, peak-to-trough (includes unrealized P&L)

    def update(self, equity: float) -> float:
        """Record the latest total equity (cash + positions) and update the drawdown."""
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        self.max_drawdown = max(self.max_drawdown, self.peak_equity - equity)
        return self.max_drawdown

    @property
    def drawdown_pct(self) -> float:
        return (self.max_drawdown / self.peak_equity * 100.0) if self.peak_equity > 0 else 0.0


def is_circuit_locked(
    change_percent: float, band_pct: float = NSE_DEFAULT_CIRCUIT_BAND_PCT
) -> bool:
    """
    True if the day's move is at/through the circuit band (no reliable fills near the limit).

    A band of <= 0 disables the check.
    """
    if band_pct <= 0:
        return False
    return abs(change_percent) >= band_pct
