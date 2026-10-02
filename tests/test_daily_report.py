"""Plan M8.4: the daily report - every audit §Y.2 field, net AI value, veto precision, output."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from src.domain.calendar import get_calendar
from src.domain.events import LLMCall, LLMOutcome
from src.domain.types import Verdict
from src.evaluation.daily_report import (
    ReportInputs,
    bootstrap,
    build_report,
    max_drawdown,
    render_markdown,
    send_summary,
    sharpe,
    telegram_summary,
    wilson,
    write_report,
)
from src.features.technical import Features
from src.strategies.policy import Proposal
from tests.test_books import run as run_day
from tests.test_books import three_books
from tests.test_engine_replay import DAY, TCS

ADVISORS = {"A": "none", "B": "typed_veto", "C": "llm_veto"}

# Audit §Y.2, field by field → where the report carries it (per book unless noted).
Y2_FIELDS = {
    "starting equity": ("capital", "start_equity"), "ending equity": ("capital", "end_equity"),
    "cash": ("capital", "cash"), "gross exposure": ("capital", "gross_exposure"),
    "net exposure": ("capital", "net_exposure"),
    "max intraday drawdown": ("capital", "max_intraday_drawdown_pct"),
    "realised (net)": ("pnl", "realized_net"), "unrealised": ("pnl", "unrealized"),
    "day return %": ("pnl", "day_return_pct"), "cumulative return %": ("pnl", "cumulative_return_pct"),
    "excess return": ("pnl", "excess_vs_nifty_day_pct"),
    "entries": ("trades", "entries"), "exits": ("trades", "exits"),
    "by strategy": ("trades", "month_to_date", "by_strategy"),
    "win rate": ("trades", "month_to_date", "win_rate"),
    "average gain": ("trades", "month_to_date", "avg_gain"),
    "average loss": ("trades", "month_to_date", "avg_loss"),
    "expectancy ₹": ("trades", "month_to_date", "expectancy_inr"),
    "expectancy %": ("trades", "month_to_date", "expectancy_pct"),
    "profit factor": ("trades", "month_to_date", "profit_factor"),
    "average holding period": ("trades", "month_to_date", "avg_holding_days"),
    "sharpe": ("risk_adjusted_mtd", "sharpe"), "sharpe CI": ("risk_adjusted_mtd", "sharpe_ci95"),
    "sortino": ("risk_adjusted_mtd", "sortino"), "max drawdown": ("risk_adjusted_mtd", "max_drawdown_pct"),
    "calmar": ("risk_adjusted_mtd", "calmar"),
    "cost components": ("costs", "today", "components"), "total cost": ("costs", "today", "total"),
    "cost bps of turnover": ("costs", "today", "bps_of_turnover"), "turnover": ("costs", "today", "turnover"),
    "slippage vs decision": ("execution", "slippage_vs_decision_bps"),
    "slippage vs arrival": ("execution", "slippage_vs_arrival_bps"),
    "implementation shortfall": ("execution", "implementation_shortfall_inr"),
    "decision → order latency": ("execution", "decision_to_order_s"),
    "order → fill latency": ("execution", "order_to_fill_s"),
    "risk rejects by reason": ("rejections", "risk_by_reason"),
    "LLM vetoes": ("rejections", "advisor_vetoes"), "broker rejects": ("rejections", "broker_by_reason"),
    "missed signals": ("rejections", "missed_signals"),
    "LLM calls": ("ai", "llm_calls"), "tokens": ("ai", "tokens_in"), "₹ spend": ("ai", "spend_inr_today"),
    "fallback rate": ("ai", "fallback_rate"), "timeouts": ("ai", "timeouts"),
    "schema failures": ("ai", "schema_failures"), "vetoes": ("ai", "verdicts"),
    "veto precision (CI)": ("ai", "veto_precision", "ci95"),
    "spend per decision": ("ai", "spend_per_decision_inr"),
    "spend per executed trade": ("ai", "spend_per_executed_trade_inr"),
    "explainable trades": ("integrity", "explained_by_decision_id"),
    "store vs engine reconciliation": ("integrity", "reconciliation_drift"),
}  # fmt: skip
Y2_GLOBAL = ("infra_cost_inr", "uptime_pct_market_hours", "downtime_minutes", "restarts",
             "loop_lag_p99_ms", "quote_age_p95_s_at_decisions", "feed_stale_events",
             "reconciliation_drift", "alerts_by_level", "kill_switch_events")  # fmt: skip

SPEND = Decimal("0.3520")


class PaidVeto:
    """B's advisor: vetoes TCS (the known loser) and pays for one LLM call per review."""

    def __init__(self, sink: Any) -> None:
        self.sink = sink

    async def review(self, proposal: Proposal, features: Features) -> Verdict:
        self.sink.emit(LLMCall(decision_id=proposal.intent.decision_id, book_id="B", role="veto",
                               provider="groq", model="llama-3.3-70b-versatile",
                               prompt_version="veto_v1", prompt_sha="x" * 64, tokens_in=1000,
                               tokens_out=200, latency_ms=800.0, cost_usd=Decimal("0.004"),
                               cost_inr=SPEND, outcome=LLMOutcome.OK), source="llm")  # fmt: skip
        return Verdict.VETO if proposal.intent.instrument.key == TCS.key else Verdict.APPROVE


def inputs(**kw: Any) -> ReportInputs:
    base: dict[str, Any] = {"day": DAY, "capital": Decimal(1_000_000), "books": ("A", "B", "C"),
                            "advisors": ADVISORS, "experiment": "test",
                            "nifty_closes": {DAY.replace(day=1): 25_000.0, DAY: 25_100.0},
                            "equal_weight_day_pct": 0.42, "infra_cost_inr_per_day": 0.0}  # fmt: skip
    base.update(kw)
    return ReportInputs(**base)


LOSS: dict[str, float] = {}


async def report_of_the_tape(tmp_path) -> dict[str, Any]:
    with three_books(tmp_path) as (engine, clock):
        engine.set_advisor("B", PaidVeto(engine.sink))
        await run_day(engine, clock)
        (row,) = engine.store.query(
            "SELECT net_pnl FROM trades WHERE book_id = 'A' AND instrument_key = ?", (TCS.key,)
        )
        LOSS["A_TCS"] = float(Decimal(row["net_pnl"]))
        return build_report(engine.store, inputs(), get_calendar())


async def test_acceptance_b_beats_a_by_the_vetoed_loser_net_of_ai_cost(tmp_path):
    report = await report_of_the_tape(tmp_path)
    loser = report["books"]["A"]["trades"]["today"]["by_strategy"]  # A took both trades
    a_trades = {k: v for k, v in loser.items()}
    assert a_trades["momentum"]["count"] == 2
    b = report["comparison"]["B"]
    reviews = report["books"]["B"]["ai"]["llm_calls"]
    assert reviews == 2  # one paid review each for INFY and TCS
    spend = float(SPEND * reviews)
    tcs_loss = report["books"]["B"]["ai"]["veto_precision"]["loss_avoided_inr"]
    assert b["ai_spend_inr_to_date"] == pytest.approx(spend)
    # The vetoed counterfactual and A's real TCS trade are sized and filled differently, so the
    # exact identity is against A's realised TCS loss: B − A = −loss, then minus the AI spend.
    a_minus_b = (
        report["books"]["A"]["capital"]["end_equity"]
        - report["books"]["B"]["capital"]["end_equity"]
    )
    assert b["equity_difference_inr"] == pytest.approx(-a_minus_b)
    assert b["net_ai_value_inr"] == pytest.approx(b["equity_difference_inr"] - spend)
    assert b["equity_difference_inr"] == pytest.approx(-LOSS["A_TCS"], abs=0.005)  # exactly
    assert b["net_ai_value_inr"] == pytest.approx(-LOSS["A_TCS"] - spend, abs=0.005)
    assert b["equity_difference_inr"] > 0 and tcs_loss > 0
    assert report["comparison"]["C"]["equity_difference_inr"] == 0  # C abstained (no advisor set)


async def test_every_y2_field_is_present_and_the_markdown_renders(tmp_path):
    report = await report_of_the_tape(tmp_path)
    for book in ("A", "B", "C"):
        for name, path in Y2_FIELDS.items():
            node: Any = report["books"][book]
            for key in path:
                assert isinstance(node, dict) and key in node, f"{book}: {name} missing at {path}"
                node = node[key]
    assert set(Y2_GLOBAL) <= set(report["infrastructure"])
    assert {"nifty_day_pct", "equal_weight_day_pct"} <= set(report["benchmark"])
    a = report["books"]["A"]
    assert a["trades"]["entries"] == 2 and a["trades"]["exits"] == 2
    assert a["costs"]["today"]["total"] > 0 and a["costs"]["today"]["components"]
    assert (
        a["execution"]["filled_orders"] >= 4
        and a["execution"]["order_to_fill_s"]["p50"] is not None
    )
    assert a["integrity"] == {"closed_trades": 2, "explained_by_decision_id": 2,
                              "reconciliations": a["integrity"]["reconciliations"],
                              "reconciliation_drift": 0}  # fmt: skip
    assert report["benchmark"]["nifty_day_pct"] == pytest.approx(0.4, abs=1e-3)
    infra = report["infrastructure"]
    assert infra["uptime_pct_market_hours"] > 90 and infra["loop_lag_p99_ms"] is not None
    b_ai = report["books"]["B"]["ai"]
    assert b_ai["veto_precision"]["precision"] == 1.0 and b_ai["veto_precision"]["settled"] == 1
    assert b_ai["veto_precision"]["ci95"] == wilson(1, 1)
    missed = report["books"]["B"]["rejections"]["missed_signals"]
    assert any(k.startswith("vetoed") for k in missed)
    md = render_markdown(report)
    for section in ("## Capital", "## P&L", "## Trades", "## Risk-adjusted", "## Costs",
                    "## Execution", "## Rejections", "## AI", "## Integrity", "## Net AI value",
                    "## Benchmark", "## Infrastructure"):  # fmt: skip
        assert section in md
    assert "| Book A | Book B | Book C |" in md.replace("| | ", "| ")


async def test_the_report_is_written_and_summarised(tmp_path):
    report = await report_of_the_tape(tmp_path)
    json_path, md_path = write_report(report, tmp_path / "reports")
    assert json_path.name == "2026-10-05.json" and md_path.name == "2026-10-05.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["date"] == "2026-10-05"
    summary = telegram_summary(report)
    assert summary.startswith("RakshaQuant 2026-10-05") and "net AI value B−A" in summary

    async def slow(text: str) -> bool:
        await asyncio.sleep(10)
        return True

    async def broken(text: str) -> bool:
        raise RuntimeError("telegram down")

    assert await send_summary(summary, slow, timeout_s=0.05) is False
    assert await send_summary(summary, broken) is False


def test_statistics():
    assert wilson(0, 0) is None and wilson(8, 10) == (0.4902, 0.9433)
    assert max_drawdown([100, 110, 99, 120]) == pytest.approx(0.1)
    assert sharpe([0.01]) is None and sharpe([0.01, 0.01]) is None
    assert sharpe([0.01, -0.005, 0.02, 0.0]) == pytest.approx(8.949, abs=1e-3)
    returns = [0.01, -0.004, 0.006, 0.002, -0.001, 0.008]
    ci = bootstrap(returns, sharpe, 500)
    assert ci is not None and ci[0] < (sharpe(returns) or 0) < ci[1]
    assert bootstrap(returns, sharpe, 500) == ci  # seeded: reproducible
    assert bootstrap([0.1, 0.2], sharpe, 500) is None
