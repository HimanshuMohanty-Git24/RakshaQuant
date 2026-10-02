"""
How much of each portfolio limit a book is using (plan M10: the Risk Center's meters).

The definitions are the RiskEngine's own (``src/risk/checks/portfolio.py``), so a meter at 100%
is exactly where the matching check starts blocking new entries: daily loss against
``daily_loss_limit_pct`` of start-of-day equity, drawdown from the peak, gross exposure, heat
(risk to the stops) and each sector against their share of equity, open positions and entries
against their counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from src.config.limits import RiskLimits
from src.risk.snapshot import UNKNOWN_SECTOR, RiskSnapshot

Unit = Literal["inr", "ratio", "count"]


@dataclass(frozen=True)
class Usage:
    key: str  # the reason code the limit is enforced with (sectors: PF_SECTOR:<name>)
    label: str
    used: Decimal
    limit: Decimal
    unit: Unit

    @property
    def fraction(self) -> float | None:
        return float(self.used / self.limit) if self.limit > 0 else None


def _of_equity(snapshot: RiskSnapshot, fraction: float) -> Decimal:
    return snapshot.equity * Decimal(str(fraction))


def utilisation(snapshot: RiskSnapshot, limits: RiskLimits) -> list[Usage]:
    s = snapshot
    held = sum(1 for p in s.positions.values() if p.quantity)
    drawdown = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else Decimal(0)
    return [
        Usage("PF_DAILY_LOSS_MTM", "Daily loss", max(Decimal(0), -s.day_pnl_mtm),
              s.sod_equity * Decimal(str(limits.daily_loss_limit_pct)), "inr"),
        Usage("PF_DRAWDOWN", "Drawdown", max(Decimal(0), drawdown),
              Decimal(str(limits.max_drawdown_pct)), "ratio"),
        Usage("PF_GROSS", "Gross exposure", s.gross_exposure,
              _of_equity(s, limits.max_gross_exposure_pct), "inr"),
        Usage("PF_HEAT", "Heat (to stops)", s.heat, _of_equity(s, limits.max_heat_pct), "inr"),
        Usage("PF_MAX_POSITIONS", "Positions", Decimal(held), Decimal(limits.max_positions),
              "count"),
        Usage("PF_ENTRIES_PER_DAY", "Entries today", Decimal(s.entries_today),
              Decimal(limits.max_entries_per_day), "count"),
    ]  # fmt: skip


def sector_usage(snapshot: RiskSnapshot, limits: RiskLimits) -> list[Usage]:
    """Each sector the book holds, largest first."""
    sectors = {p.sector or UNKNOWN_SECTOR for p in snapshot.positions.values() if p.quantity}
    limit = _of_equity(snapshot, limits.max_sector_pct)
    usages = [Usage(f"PF_SECTOR:{name}", name, snapshot.sector_exposure(name), limit, "inr")
              for name in sectors]  # fmt: skip
    return sorted(usages, key=lambda u: (-u.used, u.label))
