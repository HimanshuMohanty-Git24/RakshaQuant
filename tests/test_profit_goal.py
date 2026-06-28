"""
Tests for the profit-target goal engine.

Covers plan derivation (required win-rate / trade-frequency), the risk guardrails that
flag a target as infeasible rather than recommending more risk, and pace tracking.
"""

from datetime import datetime
from types import SimpleNamespace

from src.profit.goal_engine import ProfitGoalEngine
from src.utils.market_time import IST


def _settings(**overrides):
    base = dict(
        monthly_profit_target_pct=0.0,
        monthly_profit_target_amount=0.0,
        trading_days_per_month=21,
        expected_trades_per_day=5,
        goal_assumed_win_rate=0.5,
        goal_reward_risk_ratio=1.5,
        goal_off_pace_tolerance=0.2,
        risk_per_trade=0.01,
        daily_loss_limit=50000.0,
        max_daily_trades=50,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


CAPITAL = 1_000_000.0


# ---------------------------------------------------------------------------
# Plan derivation
# ---------------------------------------------------------------------------

def test_plan_disabled_without_target():
    engine = ProfitGoalEngine(settings=_settings())
    plan = engine.build_plan(CAPITAL)
    assert plan.enabled is False
    assert plan.feasible is False


def test_feasible_plan_derives_required_win_rate():
    # 3%/mo on 1M = 30k; risk 1% = 10k/trade, RR 1.5, 5 trades/day.
    engine = ProfitGoalEngine(settings=_settings(monthly_profit_target_pct=0.03))
    plan = engine.build_plan(CAPITAL)

    assert plan.enabled is True
    assert plan.feasible is True
    assert plan.monthly_target_amount == 30_000.0
    assert round(plan.daily_target_amount, 2) == round(30_000 / 21, 2)
    assert plan.risk_per_trade_amount == 10_000.0
    assert plan.avg_win == 15_000.0
    # required win rate ~ (285.7 + 10000) / 25000 ≈ 0.41
    assert 0.40 <= plan.required_win_rate <= 0.42
    assert plan.required_trades_per_day < plan.max_trades_within_risk
    assert "On plan" in plan.recommended_action


def test_infeasible_when_required_win_rate_too_high():
    # Huge target forces an unrealistic win rate.
    engine = ProfitGoalEngine(settings=_settings(monthly_profit_target_pct=2.0))
    plan = engine.build_plan(CAPITAL)
    assert plan.feasible is False
    assert plan.required_win_rate > 0.85
    assert any("win rate" in w for w in plan.warnings)
    assert "Lower the monthly target" in plan.recommended_action


def test_infeasible_when_trades_exceed_risk_budget():
    # risk 1% = 10k, daily-loss limit 10k -> at most 1 trade/day fits the loss budget.
    engine = ProfitGoalEngine(
        settings=_settings(
            monthly_profit_target_amount=210_000.0,
            risk_per_trade=0.01,
            daily_loss_limit=10_000.0,
        )
    )
    plan = engine.build_plan(CAPITAL)
    assert plan.max_trades_within_risk == 1
    assert plan.feasible is False
    assert any("trades/day" in w for w in plan.warnings)
    assert "do NOT increase" in plan.recommended_action


def test_infeasible_when_expected_value_non_positive():
    # 30% win rate at 1:1 reward:risk -> negative expectancy.
    engine = ProfitGoalEngine(
        settings=_settings(
            monthly_profit_target_pct=0.03,
            goal_assumed_win_rate=0.3,
            goal_reward_risk_ratio=1.0,
        )
    )
    plan = engine.build_plan(CAPITAL)
    assert plan.feasible is False
    assert plan.required_trades_per_day == float("inf")
    assert any("expected value" in w.lower() for w in plan.warnings)


# ---------------------------------------------------------------------------
# Pace tracking
# ---------------------------------------------------------------------------

def test_evaluate_on_pace():
    engine = ProfitGoalEngine(settings=_settings(monthly_profit_target_pct=0.03))
    # daily target ≈ 1428.57; 10 days -> expected ≈ 14285.7; threshold (20% tol) ≈ 11428.6
    report = engine.evaluate(CAPITAL, realized_pnl=15_000.0, elapsed_trading_days=10)
    assert report["enabled"] is True
    assert report["on_pace"] is True
    assert report["variance"] > 0
    assert report["status"] == "on pace"


def test_evaluate_behind_pace_warns_not_to_raise_risk():
    engine = ProfitGoalEngine(settings=_settings(monthly_profit_target_pct=0.03))
    report = engine.evaluate(CAPITAL, realized_pnl=5_000.0, elapsed_trading_days=10)
    assert report["on_pace"] is False
    assert "do NOT increase risk" in report["status"]
    # Guardrail: it must never recommend taking more risk.
    assert "increase risk" in report["status"].lower()


def test_evaluate_disabled_without_target():
    engine = ProfitGoalEngine(settings=_settings())
    report = engine.evaluate(CAPITAL, realized_pnl=1000.0, elapsed_trading_days=5)
    assert report["enabled"] is False


def test_evaluate_projection():
    engine = ProfitGoalEngine(settings=_settings(monthly_profit_target_pct=0.03))
    report = engine.evaluate(CAPITAL, realized_pnl=10_000.0, elapsed_trading_days=10)
    # run-rate projection to full month (21 days): 10000/10 * 21 = 21000
    assert round(report["projected_month_end"], 0) == 21_000.0


def test_elapsed_trading_days_counts_weekdays_capped():
    engine = ProfitGoalEngine(settings=_settings())
    # 2024-01-15 is a Monday; weekdays Jan 1..15 = 11.
    elapsed = engine._elapsed_trading_days(datetime(2024, 1, 15, 12, 0, tzinfo=IST))
    assert elapsed == 11
