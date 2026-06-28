"""
Profit-target goal engine.

Turns a configurable monthly return target into a concrete, risk-bounded plan:
- the daily profit it implies,
- the win-rate it needs at the expected trade frequency,
- the trade frequency it needs at an assumed win-rate,
and tracks whether the system is on/off pace.

**Guardrail (critical):** this engine is purely *advisory*. It never feeds back into
position sizing and never relaxes the risk limits. If a target can only be reached by
exceeding the per-trade risk, the daily-loss limit, or the max-daily-trades cap, the plan
is flagged ``feasible=False`` and the recommended action is to *lower the target* — never
to take more risk. Chasing the number must not breach risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.config import Settings, get_settings
from src.utils.market_time import now_ist

# A win-rate above this is treated as unrealistic for a retail momentum system.
_MAX_REALISTIC_WIN_RATE = 0.85


@dataclass
class GoalPlan:
    """The risk-bounded plan derived from the configured target."""

    enabled: bool
    capital: float
    monthly_target_amount: float
    daily_target_amount: float
    trading_days_per_month: int
    risk_per_trade_amount: float
    avg_win: float
    avg_loss: float
    reward_risk_ratio: float
    assumed_win_rate: float
    expected_trades_per_day: int
    required_win_rate: float
    required_trades_per_day: float
    max_trades_within_risk: int
    feasible: bool
    warnings: list[str] = field(default_factory=list)
    recommended_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "capital": self.capital,
            "monthly_target_amount": self.monthly_target_amount,
            "daily_target_amount": self.daily_target_amount,
            "trading_days_per_month": self.trading_days_per_month,
            "risk_per_trade_amount": self.risk_per_trade_amount,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "reward_risk_ratio": self.reward_risk_ratio,
            "assumed_win_rate": self.assumed_win_rate,
            "expected_trades_per_day": self.expected_trades_per_day,
            "required_win_rate": self.required_win_rate,
            "required_trades_per_day": self.required_trades_per_day,
            "max_trades_within_risk": self.max_trades_within_risk,
            "feasible": self.feasible,
            "warnings": list(self.warnings),
            "recommended_action": self.recommended_action,
        }


class ProfitGoalEngine:
    """Derives a risk-bounded plan from the target and tracks pace against it."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # -- Plan ---------------------------------------------------------------

    def _target_amount(self, capital: float) -> float:
        pct = float(getattr(self.settings, "monthly_profit_target_pct", 0.0) or 0.0)
        pct_target = pct * capital
        amount_target = float(getattr(self.settings, "monthly_profit_target_amount", 0.0) or 0.0)
        return max(pct_target, amount_target)

    def build_plan(self, capital: float) -> GoalPlan:
        s = self.settings
        days = max(1, int(getattr(s, "trading_days_per_month", 21)))
        rr = float(getattr(s, "goal_reward_risk_ratio", 1.5) or 1.5)
        assumed_wr = float(getattr(s, "goal_assumed_win_rate", 0.5) or 0.5)
        expected_tpd = max(1, int(getattr(s, "expected_trades_per_day", 5)))
        risk_per_trade = float(getattr(s, "risk_per_trade", 0.02) or 0.0)
        daily_loss_limit = float(getattr(s, "daily_loss_limit", 0.0) or 0.0)
        max_daily_trades = int(getattr(s, "max_daily_trades", 0) or 0)

        target = self._target_amount(capital)
        risk_amt = max(0.0, risk_per_trade * capital)
        avg_loss = risk_amt
        avg_win = rr * risk_amt
        daily_target = target / days if days else 0.0

        # Trades/day whose cumulative worst-case loss still fits the daily-loss limit.
        if risk_amt > 0 and daily_loss_limit > 0:
            max_trades_within_risk = int(daily_loss_limit // risk_amt)
        else:
            max_trades_within_risk = max_daily_trades or 0
        if max_daily_trades > 0:
            max_trades_within_risk = min(max_trades_within_risk, max_daily_trades)

        warnings: list[str] = []

        if target <= 0:
            return GoalPlan(
                enabled=False, capital=capital, monthly_target_amount=0.0,
                daily_target_amount=0.0, trading_days_per_month=days,
                risk_per_trade_amount=risk_amt, avg_win=avg_win, avg_loss=avg_loss,
                reward_risk_ratio=rr, assumed_win_rate=assumed_wr,
                expected_trades_per_day=expected_tpd, required_win_rate=0.0,
                required_trades_per_day=0.0, max_trades_within_risk=max_trades_within_risk,
                feasible=False, warnings=["No profit target configured (disabled)."],
                recommended_action="Set monthly_profit_target_pct or _amount to enable.",
            )

        if risk_amt <= 0:
            return GoalPlan(
                enabled=True, capital=capital, monthly_target_amount=target,
                daily_target_amount=daily_target, trading_days_per_month=days,
                risk_per_trade_amount=0.0, avg_win=0.0, avg_loss=0.0,
                reward_risk_ratio=rr, assumed_win_rate=assumed_wr,
                expected_trades_per_day=expected_tpd, required_win_rate=float("inf"),
                required_trades_per_day=float("inf"),
                max_trades_within_risk=max_trades_within_risk, feasible=False,
                warnings=["risk_per_trade or capital is zero; cannot plan within a risk budget."],
                recommended_action="Configure a positive risk_per_trade and capital.",
            )

        # Required win rate at the expected trade frequency:
        # EV = w*avg_win - (1-w)*avg_loss; solve for w given EV = daily_target / trades.
        ev_needed_per_trade = daily_target / expected_tpd
        required_win_rate = (ev_needed_per_trade + avg_loss) / (avg_win + avg_loss)

        # Required trades/day at the assumed win rate.
        ev_per_trade_assumed = assumed_wr * avg_win - (1 - assumed_wr) * avg_loss
        if ev_per_trade_assumed > 0:
            required_trades_per_day = daily_target / ev_per_trade_assumed
        else:
            required_trades_per_day = float("inf")
            warnings.append(
                f"At the assumed {assumed_wr:.0%} win rate and {rr:.1f}:1 reward:risk, "
                "expected value per trade is <= 0 — the target cannot be reached at any "
                "trade frequency without taking more risk."
            )

        feasible = True
        if required_win_rate > _MAX_REALISTIC_WIN_RATE:
            feasible = False
            warnings.append(
                f"Target needs a {required_win_rate:.0%} win rate at {expected_tpd} trades/day — "
                f"above the {_MAX_REALISTIC_WIN_RATE:.0%} realism ceiling."
            )
        if math.isinf(required_trades_per_day) or required_trades_per_day > max_trades_within_risk:
            feasible = False
            shown = "∞" if math.isinf(required_trades_per_day) else f"{required_trades_per_day:.1f}"
            warnings.append(
                f"Target needs ~{shown} trades/day but risk limits allow only "
                f"{max_trades_within_risk} (daily-loss limit / max-daily-trades)."
            )

        # The only safe lever is the target itself — never the risk.
        if feasible:
            recommended_action = "On plan — keep position sizing within configured risk limits."
        else:
            achievable_daily = max(0.0, ev_per_trade_assumed) * max_trades_within_risk
            achievable_monthly = achievable_daily * days
            recommended_action = (
                "Lower the monthly target to about "
                f"Rs.{achievable_monthly:,.0f} (achievable within the current risk budget) — "
                "do NOT increase risk_per_trade or trade frequency to chase it."
            )

        return GoalPlan(
            enabled=True, capital=capital, monthly_target_amount=target,
            daily_target_amount=daily_target, trading_days_per_month=days,
            risk_per_trade_amount=risk_amt, avg_win=avg_win, avg_loss=avg_loss,
            reward_risk_ratio=rr, assumed_win_rate=assumed_wr,
            expected_trades_per_day=expected_tpd,
            required_win_rate=required_win_rate,
            required_trades_per_day=required_trades_per_day,
            max_trades_within_risk=max_trades_within_risk, feasible=feasible,
            warnings=warnings, recommended_action=recommended_action,
        )

    # -- Pace tracking ------------------------------------------------------

    def _elapsed_trading_days(self, ref: datetime | None = None) -> int:
        """Weekdays elapsed in the current month (IST), capped at trading_days_per_month."""
        ref = ref or now_ist()
        days = int(getattr(self.settings, "trading_days_per_month", 21))
        count = 0
        cursor = ref.replace(day=1)
        while cursor.date() <= ref.date():
            if cursor.weekday() < 5:
                count += 1
            cursor += timedelta(days=1)
        return max(0, min(count, days))

    def evaluate(
        self,
        capital: float,
        realized_pnl: float,
        elapsed_trading_days: int | None = None,
    ) -> dict[str, Any]:
        """
        Report pace vs the plan given month-to-date realized P&L.

        Never recommends taking more risk; if behind pace it says so explicitly.
        """
        plan = self.build_plan(capital)
        if not plan.enabled:
            return {"enabled": False, "plan": plan.to_dict()}

        if elapsed_trading_days is not None:
            elapsed = elapsed_trading_days
        else:
            elapsed = self._elapsed_trading_days()
        elapsed = max(0, min(elapsed, plan.trading_days_per_month))
        expected_to_date = plan.daily_target_amount * elapsed
        tolerance = float(getattr(self.settings, "goal_off_pace_tolerance", 0.2) or 0.0)

        # On pace if MTD P&L is within tolerance of the straight-line pace target.
        threshold = expected_to_date * (1 - tolerance)
        on_pace = realized_pnl >= threshold
        variance = realized_pnl - expected_to_date

        if elapsed > 0:
            projected_month_end = (realized_pnl / elapsed) * plan.trading_days_per_month
        else:
            projected_month_end = 0.0
        if plan.monthly_target_amount:
            pct_of_target = realized_pnl / plan.monthly_target_amount
        else:
            pct_of_target = 0.0

        if not plan.feasible:
            status = "target not feasible within risk budget"
        elif on_pace:
            status = "on pace"
        else:
            status = "behind pace — do NOT increase risk; review whether the target is realistic"

        return {
            "enabled": True,
            "feasible": plan.feasible,
            "on_pace": bool(on_pace),
            "status": status,
            "monthly_target_amount": plan.monthly_target_amount,
            "daily_target_amount": plan.daily_target_amount,
            "elapsed_trading_days": elapsed,
            "expected_to_date": expected_to_date,
            "month_to_date_pnl": realized_pnl,
            "variance": variance,
            "projected_month_end": projected_month_end,
            "pct_of_target": pct_of_target,
            "plan": plan.to_dict(),
        }
