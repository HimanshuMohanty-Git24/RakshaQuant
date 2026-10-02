"""
The daily report (plan M8.4; audit §Y.2), generated from the event store alone, so any day can be
regenerated offline and every number traces back to recorded events.

Per book, for the day and month to date: capital, P&L (incl. benchmark and excess return),
trades, risk-adjusted figures with bootstrap CIs, costs, execution quality, rejections and missed
signals, AI usage and value, and integrity. Across books: **net AI value** (B−A and C−A, minus
that book's AI spend), **veto precision** per advisor (vetoed signals whose counterfactual lost,
with a Wilson CI), fallback/ABSTAIN and decision-model escalation rates, and infrastructure.

Fields the system cannot observe are reported as ``None`` ("not recorded"), never invented.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Generic, TypeVar

import numpy as np

from src.domain.base import EventPayload
from src.domain.calendar import CalendarCoverageError, NSECalendar
from src.domain.events import (
    AdvisorFallback,
    AdvisorRequested,
    Alert,
    AnnouncementCoverageGap,
    DailyRiskStateRolled,
    DecisionModelCall,
    Event,
    FeedStale,
    FillReceived,
    Heartbeat,
    KillSwitchChanged,
    LLMCall,
    MarkToMarket,
    OrderRejected,
    OrderSubmitted,
    ProcessStarted,
    ReconciliationResult,
    ShadowTradeClosed,
    SignalDisposition,
    SignalGenerated,
    TradeClosed,
)
from src.domain.types import AdvisorVerdict, RiskDecision, RiskOutcome, Side
from src.store.event_store import EventStore
from src.utils.market_time import IST

logger = logging.getLogger(__name__)
P = TypeVar("P", bound=EventPayload)

SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)
_CODE = re.compile(r"\b(?:ORD|STR|PF|SYS|EVT)_[A-Z_]+\b")


@dataclass(frozen=True)
class ReportInputs:
    day: date
    capital: Decimal
    books: tuple[str, ...]
    advisors: Mapping[str, str]  # book -> "none" | "typed_veto" | "llm_veto"
    start: date | None = None  # experiment start (default: the first recorded day)
    experiment: str = ""
    nifty_closes: Mapping[date, float] = field(default_factory=dict)
    equal_weight_day_pct: float | None = None
    infra_cost_inr_per_day: float | None = None
    intraday_low_equity: Mapping[str, float] = field(default_factory=dict)  # live, at report time
    bootstrap_samples: int = 2000


# -- statistics ----------------------------------------------------------------------------------


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(centre - half, 4), round(centre + half, 4)


def sharpe(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    sd = float(np.std(returns, ddof=1))
    return None if sd == 0 else float(np.mean(returns) / sd * math.sqrt(252))


def sortino(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    downside = [min(0.0, r) for r in returns]
    dd = math.sqrt(sum(d * d for d in downside) / len(returns))
    return None if dd == 0 else float(np.mean(returns) / dd * math.sqrt(252))


def max_drawdown(equity: Sequence[float]) -> float:
    peak, worst = -math.inf, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def bootstrap(
    values: Sequence[float], statistic: Any, samples: int, seed: int = 7
) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    rng = np.random.default_rng(seed)
    data = np.asarray(values, dtype=float)
    stats = []
    for _ in range(samples):
        value = statistic(list(rng.choice(data, size=len(data), replace=True)))
        if value is not None and math.isfinite(value):
            stats.append(value)
    if not stats:
        return None
    return round(float(np.percentile(stats, 2.5)), 4), round(float(np.percentile(stats, 97.5)), 4)


def _pct(values: Sequence[float], q: float) -> float | None:
    return round(float(np.percentile(values, q)), 2) if values else None


def _f(value: Any, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


# -- the report ----------------------------------------------------------------------------------


class _E(Generic[P]):
    """One event with its payload typed."""

    __slots__ = ("event", "payload")

    def __init__(self, event: Event, payload: P) -> None:
        self.event, self.payload = event, payload

    @property
    def ist_date(self) -> date:
        return self.event.ist_date

    @property
    def ts_utc(self) -> datetime:
        return self.event.ts_utc

    @property
    def book_id(self) -> str | None:
        return self.event.book_id


class _Events:
    """The store's events, indexed once."""

    def __init__(self, events: Iterable[Event]) -> None:
        self.all = list(events)
        self.by_type: dict[str, list[Event]] = defaultdict(list)
        for event in self.all:
            self.by_type[event.type].append(event)

    def of(self, kind: type[P], *, book: str | None = None, day: date | None = None,
           until: date | None = None, since: date | None = None) -> list[_E[P]]:  # fmt: skip
        out: list[_E[P]] = []
        for e in self.by_type.get(kind.event_type, ()):
            if book is not None and e.book_id != book:
                continue
            if day is not None and e.ist_date != day:
                continue
            if until is not None and e.ist_date > until:
                continue
            if since is not None and e.ist_date < since:
                continue
            if isinstance(e.payload, kind):
                out.append(_E(e, e.payload))
        return out


def build_report(store: EventStore, inputs: ReportInputs, calendar: NSECalendar) -> dict[str, Any]:
    ev = _Events(store.read())
    day = inputs.day
    days = sorted({e.ist_date for e in ev.all if e.ist_date <= day})
    start = inputs.start or (days[0] if days else day)
    books = {b: _book(ev, b, inputs, start, calendar) for b in inputs.books}
    primary = inputs.books[0]
    comparison = {
        b: _comparison(ev, b, primary, books, inputs, start)
        for b in inputs.books if b != primary
    }  # fmt: skip
    return {
        "experiment": inputs.experiment,
        "date": day.isoformat(),
        "start": start.isoformat(),
        "sessions_to_date": len([d for d in days if d >= start]),
        "benchmark": _benchmark(inputs, start, calendar),
        "books": books,
        "comparison": comparison,
        "infrastructure": _infrastructure(ev, inputs),
    }


def _equity_by_day(ev: _Events, book: str, start: date, day: date) -> dict[date, float]:
    out: dict[date, float] = {}
    for e in ev.of(MarkToMarket, book=book, since=start, until=day):
        out[e.ist_date] = float(e.payload.equity)  # the last mark of each day
    return out


def _book(
    ev: _Events, book: str, inputs: ReportInputs, start: date, calendar: NSECalendar
) -> dict[str, Any]:
    day, capital = inputs.day, float(inputs.capital)
    equity = _equity_by_day(ev, book, start, day)
    marks_today = ev.of(MarkToMarket, book=book, day=day)
    mtm = marks_today[-1].payload if marks_today else None
    previous = [v for d, v in sorted(equity.items()) if d < day]
    start_equity = previous[-1] if previous else capital
    end_equity = float(mtm.equity) if mtm is not None else None
    series = [capital, *[v for _, v in sorted(equity.items())]]
    returns = [b / a - 1 for a, b in zip(series, series[1:], strict=False) if a]
    rolled = [
        e.payload for e in ev.of(DailyRiskStateRolled, book=book) if e.payload.ist_date == day
    ]
    low = inputs.intraday_low_equity.get(book)
    if low is None and rolled:  # regenerating a past day: the state rolled the next morning
        value = rolled[-1].state.get("low_equity")
        low = float(value) if isinstance(value, int | float | str) else None
    intraday_dd = max(0.0, (start_equity - low) / start_equity) if low and start_equity else None

    closed_today = [e.payload for e in ev.of(TradeClosed, book=book, day=day)]
    closed_mtd = [e.payload for e in ev.of(TradeClosed, book=book, since=start, until=day)]
    submitted = [e.payload.order for e in ev.of(OrderSubmitted, book=book, day=day)]
    fills = ev.of(FillReceived, book=book, day=day)
    fills_mtd = ev.of(FillReceived, book=book, since=start, until=day)
    net_return = (end_equity - start_equity) / start_equity if end_equity is not None else None
    cumulative = (end_equity - capital) / capital if end_equity is not None else None
    nifty = _benchmark(inputs, start, calendar)
    return {
        "advisor": inputs.advisors.get(book, "none"),
        "capital": {
            "start_equity": round(start_equity, 2),
            "end_equity": _f(end_equity),
            "cash": _f(mtm.cash) if mtm else None,
            "gross_exposure": _f(mtm.positions_value) if mtm else None,
            "net_exposure": _f(mtm.positions_value) if mtm else None,  # long-only
            "max_intraday_drawdown_pct": _f(intraday_dd * 100, 3)
            if intraday_dd is not None
            else None,
        },
        "pnl": {
            "realized_net": round(sum(float(t.net_pnl) for t in closed_today), 2),
            "unrealized": _f(mtm.unrealized_pnl) if mtm else None,
            "day_return_pct": _f(net_return * 100, 3) if net_return is not None else None,
            "cumulative_return_pct": _f(cumulative * 100, 3) if cumulative is not None else None,
            "excess_vs_nifty_day_pct": (
                _f(net_return * 100 - nifty["nifty_day_pct"], 3)
                if net_return is not None and nifty["nifty_day_pct"] is not None
                else None
            ),
            "excess_vs_equal_weight_day_pct": (
                _f(net_return * 100 - inputs.equal_weight_day_pct, 3)
                if net_return is not None and inputs.equal_weight_day_pct is not None
                else None
            ),
        },
        "trades": {
            "entries": sum(1 for o in submitted if not o.intent.kind.reduces_risk),
            "exits": len(closed_today),
            "today": _trade_stats(closed_today),
            "month_to_date": _trade_stats(closed_mtd),
        },
        "risk_adjusted_mtd": {
            "observations": len(returns),
            "sharpe": _f(sharpe(returns), 3),
            "sharpe_ci95": bootstrap(returns, sharpe, inputs.bootstrap_samples),
            "sortino": _f(sortino(returns), 3),
            "max_drawdown_pct": round(max_drawdown(series) * 100, 3),
            "calmar": _calmar(returns, series),
        },
        "costs": {"today": _costs(fills), "month_to_date": _costs(fills_mtd)},
        "execution": _execution(ev, book, day, submitted),
        "rejections": _rejections(ev, book, day),
        "ai": _ai(ev, book, inputs, start, len(closed_mtd)),
        "integrity": _integrity(ev, book, day, closed_today),
    }


def _trade_stats(trades: Sequence[Any]) -> dict[str, Any]:
    if not trades:
        return {"count": 0}
    nets = [float(t.net_pnl) for t in trades]
    rets = [
        float(t.net_pnl) / float(t.entry_price * t.quantity) * 100 for t in trades if t.quantity
    ]
    gains, losses = [n for n in nets if n > 0], [n for n in nets if n < 0]
    holding = [(t.exit_ts - t.entry_ts).total_seconds() / 86_400 for t in trades]
    by_strategy: dict[str, dict[str, float]] = {}
    for t in trades:
        row = by_strategy.setdefault(t.strategy, {"count": 0, "net_pnl": 0.0})
        row["count"] += 1
        row["net_pnl"] = round(row["net_pnl"] + float(t.net_pnl), 2)
    return {
        "count": len(trades),
        "win_rate": round(len(gains) / len(nets), 4),
        "avg_gain": _f(np.mean(gains)) if gains else None,
        "avg_loss": _f(np.mean(losses)) if losses else None,
        "expectancy_inr": _f(np.mean(nets)),
        "expectancy_pct": _f(np.mean(rets), 4) if rets else None,
        "profit_factor": _f(sum(gains) / -sum(losses), 3) if losses else None,
        "avg_holding_days": _f(np.mean(holding), 2),
        "by_strategy": by_strategy,
    }


def _calmar(returns: Sequence[float], series: Sequence[float]) -> float | None:
    dd = max_drawdown(series)
    if not returns or dd == 0:
        return None
    annual = (1 + float(np.mean(returns))) ** 252 - 1
    return _f(annual / dd, 3)


def _costs(fills: Sequence[_E[FillReceived]]) -> dict[str, Any]:
    components: dict[str, float] = defaultdict(float)
    total, turnover = Decimal(0), Decimal(0)
    for e in fills:
        fill = e.payload.fill
        total += fill.charges
        turnover += fill.price * fill.quantity
        for name, value in fill.charges_breakdown.items():
            components[name] += float(value)
    return {
        "components": {k: round(v, 2) for k, v in sorted(components.items())},
        "total": _f(total),
        "turnover": _f(turnover),
        "bps_of_turnover": _f(total / turnover * 10_000, 2) if turnover else None,
    }


def _execution(ev: _Events, book: str, day: date, submitted: Sequence[Any]) -> dict[str, Any]:
    arrival = {e.payload.client_order_id: e.payload.ref_price
               for e in ev.of(RiskDecision, book=book, day=day)}  # fmt: skip
    decided = {e.payload.signal.decision_id: e.payload.signal.generated_at
               for e in ev.of(SignalGenerated)}  # fmt: skip
    fills: dict[str, list[Any]] = defaultdict(list)
    for e in ev.of(FillReceived, book=book, day=day):
        fills[e.payload.fill.client_order_id].append(e.payload)
    vs_decision, vs_arrival, to_order, to_fill = [], [], [], []
    shortfall = Decimal(0)
    for order in submitted:
        got = fills.get(order.client_order_id)
        sign = 1 if order.intent.side is Side.BUY else -1
        if got:
            avg = got[-1].order_avg_price
            qty = got[-1].order_filled_qty
            decision = order.intent.decision_price
            vs_decision.append(float((avg / decision - 1) * sign * 10_000))
            ref = arrival.get(order.client_order_id)
            if ref:
                vs_arrival.append(float((avg / ref - 1) * sign * 10_000))
            shortfall += (avg - decision) * qty * sign + sum(f.fill.charges for f in got)
            if order.submitted_at is not None:
                to_fill.append((got[0].fill.ts - order.submitted_at).total_seconds())
        when = decided.get(order.intent.decision_id)
        if (
            when is not None
            and order.submitted_at is not None
            and not order.intent.kind.reduces_risk
        ):
            to_order.append((order.submitted_at - when).total_seconds())
    return {
        "filled_orders": len(vs_decision),
        "slippage_vs_decision_bps": {"mean": _f(np.mean(vs_decision)) if vs_decision else None,
                                     "p95": _pct(vs_decision, 95)},
        "slippage_vs_arrival_bps": {"mean": _f(np.mean(vs_arrival)) if vs_arrival else None,
                                    "p95": _pct(vs_arrival, 95)},
        "implementation_shortfall_inr": _f(shortfall),
        "decision_to_order_s": {"p50": _pct(to_order, 50), "p95": _pct(to_order, 95)},
        "order_to_fill_s": {"p50": _pct(to_fill, 50), "p95": _pct(to_fill, 95)},
    }  # fmt: skip


def _rejections(ev: _Events, book: str, day: date) -> dict[str, Any]:
    risk: Counter[str] = Counter()
    for decided in ev.of(RiskDecision, book=book, day=day):
        r = decided.payload
        if r.outcome in (RiskOutcome.REJECTED, RiskOutcome.HALTED):
            for reason in r.reasons:
                if reason.outcome.value == "block":
                    risk[reason.code.value] += 1
    broker = Counter(e.payload.reason for e in ev.of(OrderRejected, book=book, day=day))
    vetoes = Counter(e.payload.advisor.value for e in ev.of(AdvisorVerdict, book=book, day=day)
                     if e.payload.verdict.value == "VETO")  # fmt: skip
    missed: Counter[str] = Counter()
    for item in ev.of(SignalDisposition, book=book, day=day):
        d = item.payload
        if d.disposition.value in ("submitted", "shadow_strategy"):
            continue
        codes = _CODE.findall(d.detail) if d.disposition.value == "risk_rejected" else []
        for code in codes or [d.detail.split(":")[0] or d.disposition.value]:
            missed[f"{d.disposition.value}:{code}"] += 1
    return {"risk_by_reason": dict(risk.most_common()), "broker_by_reason": dict(broker),
            "advisor_vetoes": dict(vetoes), "missed_signals": dict(missed.most_common())}  # fmt: skip


def _ai(
    ev: _Events, book: str, inputs: ReportInputs, start: date, closed_mtd: int
) -> dict[str, Any]:
    day = inputs.day
    calls_today = [e.payload for e in ev.of(LLMCall, book=book, day=day)]
    calls_mtd = [e.payload for e in ev.of(LLMCall, book=book, since=start, until=day)]
    spend_mtd = sum((c.cost_inr or Decimal(0) for c in calls_mtd), Decimal(0))
    requested = len(ev.of(AdvisorRequested, book=book, day=day))
    verdicts = [e.payload for e in ev.of(AdvisorVerdict, book=book, day=day)]
    fallbacks = len(ev.of(AdvisorFallback, book=book, day=day))
    decisions_mtd = len({e.payload.decision_id
                         for e in ev.of(AdvisorRequested, book=book, since=start, until=day)})  # fmt: skip
    model_calls = [e.payload for e in ev.of(DecisionModelCall, book=book, day=day)]
    precision = veto_precision(ev, book, start, day)
    return {
        "llm_calls": len(calls_today),
        "tokens_in": sum(c.tokens_in for c in calls_today),
        "tokens_out": sum(c.tokens_out for c in calls_today),
        "spend_inr_today": _f(sum((c.cost_inr or Decimal(0) for c in calls_today), Decimal(0)), 4),
        "spend_inr_to_date": _f(spend_mtd, 4),
        "timeouts": sum(1 for c in calls_today if c.outcome.value == "timeout"),
        "schema_failures": sum(1 for c in calls_today if c.outcome.value == "invalid_output"),
        "advisor_reviews": requested,
        "verdicts": dict(Counter(v.verdict.value for v in verdicts)),
        "fallback_rate": round(fallbacks / requested, 4) if requested else None,
        "abstain_rate": (
            round(sum(1 for v in verdicts if v.verdict.value == "ABSTAIN") / len(verdicts), 4)
            if verdicts
            else None
        ),  # fmt: skip
        "decision_model_calls": len(model_calls),
        "escalation_rate": (
            round(sum(1 for c in model_calls if c.escalated) / len(model_calls), 4)
            if model_calls
            else None
        ),  # fmt: skip
        "veto_precision": precision,
        "spend_per_decision_inr": _f(spend_mtd / decisions_mtd, 4) if decisions_mtd else None,
        "spend_per_executed_trade_inr": _f(spend_mtd / closed_mtd, 4) if closed_mtd else None,
    }


def veto_precision(ev: _Events, book: str, start: date, day: date) -> dict[str, Any]:
    """Vetoed signals whose counterfactual lost money (net), with a Wilson 95% CI."""
    vetoed = {e.payload.signal_id for e in ev.of(SignalDisposition, book=book, since=start, until=day)
              if e.payload.disposition.value == "vetoed"}  # fmt: skip
    outcomes = {e.payload.signal_id: float(e.payload.net_pnl)
                for e in ev.of(ShadowTradeClosed, since=start, until=day)}  # fmt: skip
    settled = [outcomes[s] for s in vetoed if s in outcomes]
    correct = sum(1 for pnl in settled if pnl < 0)
    return {"vetoes": len(vetoed), "settled": len(settled), "correct": correct,
            "precision": round(correct / len(settled), 4) if settled else None,
            "ci95": wilson(correct, len(settled)),
            "loss_avoided_inr": round(-sum(p for p in settled if p < 0), 2),
            "gain_forgone_inr": round(sum(p for p in settled if p > 0), 2)}  # fmt: skip


def _comparison(ev: _Events, book: str, primary: str, books: Mapping[str, dict[str, Any]],
                inputs: ReportInputs, start: date) -> dict[str, Any]:  # fmt: skip
    mine, base = books[book]["capital"]["end_equity"], books[primary]["capital"]["end_equity"]
    spend = books[book]["ai"]["spend_inr_to_date"] or 0.0
    gross = round(mine - base, 2) if mine is not None and base is not None else None
    return {
        "vs": primary,
        "advisor": inputs.advisors.get(book, "none"),
        "equity_difference_inr": gross,
        "ai_spend_inr_to_date": spend,
        "net_ai_value_inr": round(gross - spend, 4) if gross is not None else None,
    }


def _benchmark(inputs: ReportInputs, start: date, calendar: NSECalendar) -> dict[str, Any]:
    closes = inputs.nifty_closes

    def before(d: date) -> date | None:
        try:
            return calendar.previous_trading_day(d)
        except CalendarCoverageError:
            return None

    prev = before(inputs.day)
    today_close = closes.get(inputs.day)
    day_pct = ((today_close / closes[prev] - 1) * 100
               if today_close is not None and prev is not None and prev in closes else None)  # fmt: skip
    base_day = before(start)
    known = [d for d in closes if d <= inputs.day]
    latest = max(known) if known else None
    cum = ((closes[latest] / closes[base_day] - 1) * 100
           if latest is not None and base_day is not None and base_day in closes else None)  # fmt: skip
    return {"nifty_day_pct": _f(day_pct, 3), "nifty_cumulative_pct": _f(cum, 3),
            "nifty_as_of": latest.isoformat() if latest else None,
            "equal_weight_day_pct": _f(inputs.equal_weight_day_pct, 3)}  # fmt: skip


def _integrity(ev: _Events, book: str, day: date, closed: Sequence[Any]) -> dict[str, Any]:
    by_decision: dict[str, set[str]] = defaultdict(set)
    for e in ev.all:
        if e.decision_id:
            by_decision[e.decision_id].add(e.type)
    explained = 0
    for trade in closed:
        types = by_decision.get(trade.decision_id, set())
        if {"RiskDecision", "OrderSubmitted", "FillReceived"} <= types and (
            "SignalGenerated" in types or "OrderIntentProposed" in types
        ):
            explained += 1
    recon = [e.payload for e in ev.of(ReconciliationResult, book=book, day=day)]
    return {"closed_trades": len(closed), "explained_by_decision_id": explained,
            "reconciliations": len(recon),
            "reconciliation_drift": sum(1 for r in recon if not r.in_sync)}  # fmt: skip


def _infrastructure(ev: _Events, inputs: ReportInputs) -> dict[str, Any]:
    day = inputs.day
    beats = [e.payload for e in ev.of(Heartbeat, day=day)
             if SESSION_OPEN <= e.ts_utc.astimezone(IST).time() < SESSION_CLOSE]  # fmt: skip
    session_s = (
        datetime.combine(day, SESSION_CLOSE) - datetime.combine(day, SESSION_OPEN)
    ).total_seconds()
    minutes_up = len({e.ts_utc.astimezone(IST).replace(second=0, microsecond=0)
                      for e in ev.of(Heartbeat, day=day)
                      if SESSION_OPEN <= e.ts_utc.astimezone(IST).time() < SESSION_CLOSE})  # fmt: skip
    lags = [b.loop_lag_ms for b in beats if b.loop_lag_ms is not None]
    ages: dict[str, list[float]] = defaultdict(list)
    for e in ev.of(RiskDecision, day=day):
        snap = e.payload.snapshot
        if snap.quote_age_s is not None:
            ages[snap.data_source.value if snap.data_source else "unknown"].append(snap.quote_age_s)
    starts = ev.of(ProcessStarted, day=day)
    alerts = Counter(e.payload.level for e in ev.of(Alert, day=day))
    return {
        "infra_cost_inr": inputs.infra_cost_inr_per_day,
        "uptime_pct_market_hours": round(min(1.0, minutes_up * 60 / session_s) * 100, 2)
        if beats
        else None,
        "downtime_minutes": round(max(0.0, session_s / 60 - minutes_up), 1) if beats else None,
        "process_starts": len(starts),
        "restarts": max(0, len(starts) - 1),
        "loop_lag_p99_ms": _pct(lags, 99),
        "quote_age_p95_s_at_decisions": {k: _pct(v, 95) for k, v in sorted(ages.items())},
        "feed_stale_events": len(ev.of(FeedStale, day=day)),
        "announcement_coverage_gaps": len(ev.of(AnnouncementCoverageGap, day=day)),
        "reconciliation_drift": sum(
            1 for e in ev.of(ReconciliationResult, day=day) if not e.payload.in_sync
        ),  # fmt: skip
        "alerts_by_level": dict(alerts),
        "kill_switch_events": [
            f"{e.payload.book_id}/{e.payload.name}: {e.payload.previous}"
            f"->{e.payload.current} ({e.payload.reason})"
            for e in ev.of(KillSwitchChanged, day=day)
        ],  # fmt: skip
    }


# -- output ----------------------------------------------------------------------------------------


def render_markdown(report: Mapping[str, Any]) -> str:
    books = report["books"]
    ids = list(books)
    lines = [f"# Daily report {report['date']} — {report['experiment'] or 'experiment'}", "",
             f"Session {report['sessions_to_date']} since {report['start']}. Generated from the event "
             "store; `—` = not recorded.", ""]  # fmt: skip

    def table(title: str, rows: Sequence[tuple[str, Sequence[str]]]) -> None:
        lines.extend([f"## {title}", "", "| | " + " | ".join(f"Book {b}" for b in ids) + " |",
                      "|---|" + "---|" * len(ids)])  # fmt: skip
        for label, path in rows:
            cells = []
            for b in ids:
                value: Any = books[b]
                for key in path:
                    value = value.get(key) if isinstance(value, Mapping) else None
                cells.append(_cell(value))
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")

    table(
        "Capital",
        [
            ("Advisor", ["advisor"]),
            ("Start equity", ["capital", "start_equity"]),
            ("End equity", ["capital", "end_equity"]),
            ("Cash", ["capital", "cash"]),
            ("Gross exposure", ["capital", "gross_exposure"]),
            ("Max intraday drawdown %", ["capital", "max_intraday_drawdown_pct"]),
        ],
    )
    table(
        "P&L",
        [
            ("Realised (net)", ["pnl", "realized_net"]),
            ("Unrealised", ["pnl", "unrealized"]),
            ("Day return %", ["pnl", "day_return_pct"]),
            ("Cumulative return %", ["pnl", "cumulative_return_pct"]),
            ("Excess vs NIFTY (day) %", ["pnl", "excess_vs_nifty_day_pct"]),
            ("Excess vs equal-weight (day) %", ["pnl", "excess_vs_equal_weight_day_pct"]),
        ],
    )
    table(
        "Trades",
        [
            ("Entries", ["trades", "entries"]),
            ("Exits", ["trades", "exits"]),
            ("Win rate (MTD)", ["trades", "month_to_date", "win_rate"]),
            ("Avg gain (MTD)", ["trades", "month_to_date", "avg_gain"]),
            ("Avg loss (MTD)", ["trades", "month_to_date", "avg_loss"]),
            ("Expectancy ₹ (MTD)", ["trades", "month_to_date", "expectancy_inr"]),
            ("Expectancy % (MTD)", ["trades", "month_to_date", "expectancy_pct"]),
            ("Profit factor (MTD)", ["trades", "month_to_date", "profit_factor"]),
            ("Avg holding days (MTD)", ["trades", "month_to_date", "avg_holding_days"]),
        ],
    )
    table("Risk-adjusted (month to date)", [
        ("Observations", ["risk_adjusted_mtd", "observations"]),
        ("Sharpe", ["risk_adjusted_mtd", "sharpe"]), ("Sharpe 95% CI", ["risk_adjusted_mtd", "sharpe_ci95"]),
        ("Sortino", ["risk_adjusted_mtd", "sortino"]),
        ("Max drawdown %", ["risk_adjusted_mtd", "max_drawdown_pct"]),
        ("Calmar", ["risk_adjusted_mtd", "calmar"])])  # fmt: skip
    table(
        "Costs (today)",
        [
            ("Total", ["costs", "today", "total"]),
            ("Turnover", ["costs", "today", "turnover"]),
            ("bps of turnover", ["costs", "today", "bps_of_turnover"]),
            ("Components", ["costs", "today", "components"]),
        ],
    )
    table("Execution (today)", [
        ("Filled orders", ["execution", "filled_orders"]),
        ("Slippage vs decision bps (mean)", ["execution", "slippage_vs_decision_bps", "mean"]),
        ("Slippage vs arrival bps (mean)", ["execution", "slippage_vs_arrival_bps", "mean"]),
        ("Implementation shortfall ₹", ["execution", "implementation_shortfall_inr"]),
        ("Decision → order s (p50)", ["execution", "decision_to_order_s", "p50"]),
        ("Order → fill s (p50)", ["execution", "order_to_fill_s", "p50"])])  # fmt: skip
    table("Rejections and missed signals (today)", [
        ("Risk rejects by reason", ["rejections", "risk_by_reason"]),
        ("Broker rejects", ["rejections", "broker_by_reason"]),
        ("Advisor vetoes", ["rejections", "advisor_vetoes"]),
        ("Missed signals", ["rejections", "missed_signals"])])  # fmt: skip
    table(
        "AI",
        [
            ("LLM calls", ["ai", "llm_calls"]),
            ("Tokens in/out", ["ai", "tokens_in"]),
            ("Spend ₹ today", ["ai", "spend_inr_today"]),
            ("Spend ₹ to date", ["ai", "spend_inr_to_date"]),
            ("Timeouts", ["ai", "timeouts"]),
            ("Schema failures", ["ai", "schema_failures"]),
            ("Verdicts", ["ai", "verdicts"]),
            ("Fallback rate", ["ai", "fallback_rate"]),
            ("Abstain rate", ["ai", "abstain_rate"]),
            ("Decision-model escalation rate", ["ai", "escalation_rate"]),
            ("Veto precision (to date)", ["ai", "veto_precision", "precision"]),
            ("Veto precision 95% CI", ["ai", "veto_precision", "ci95"]),
            ("Vetoes settled", ["ai", "veto_precision", "settled"]),
            ("Spend per decision ₹", ["ai", "spend_per_decision_inr"]),
            ("Spend per executed trade ₹", ["ai", "spend_per_executed_trade_inr"]),
        ],
    )
    table(
        "Integrity",
        [
            ("Closed trades", ["integrity", "closed_trades"]),
            ("Explained by decision_id", ["integrity", "explained_by_decision_id"]),
            ("Reconciliation drift", ["integrity", "reconciliation_drift"]),
        ],
    )
    lines.extend(["## Net AI value", "", "| Book | vs | Advisor | Equity difference ₹ | AI spend ₹ | "
                  "Net AI value ₹ |", "|---|---|---|---|---|---|"])  # fmt: skip
    for b, c in report["comparison"].items():
        lines.append(f"| {b} | {c['vs']} | {c['advisor']} | {_cell(c['equity_difference_inr'])} | "
                     f"{_cell(c['ai_spend_inr_to_date'])} | {_cell(c['net_ai_value_inr'])} |")  # fmt: skip
    bench = report["benchmark"]
    lines.extend(["", "## Benchmark", "", f"NIFTY day {_cell(bench['nifty_day_pct'])}% · cumulative "
                  f"{_cell(bench['nifty_cumulative_pct'])}% (as of {_cell(bench['nifty_as_of'])}) · "
                  f"equal-weight day {_cell(bench['equal_weight_day_pct'])}%", "",
                  "## Infrastructure", ""])  # fmt: skip
    for key, value in report["infrastructure"].items():
        lines.append(f"- {key.replace('_', ' ')}: {_cell(value)}")
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    if value is None or value == {} or value == []:
        return "—"
    if isinstance(value, float):
        return f"{value:,.4g}" if abs(value) < 10 else f"{value:,.2f}"
    if isinstance(value, Mapping):
        return ", ".join(f"{k} {_cell(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_cell(v) for v in value) + "]"
    return str(value)


def write_report(report: Mapping[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = reports_dir / str(report["date"])
    json_path, md_path = stem.with_suffix(".json"), stem.with_suffix(".md")
    for path, text in ((json_path, json.dumps(report, indent=2, default=str)),
                       (md_path, render_markdown(report))):  # fmt: skip
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    return json_path, md_path


def telegram_summary(report: Mapping[str, Any]) -> str:
    parts = [f"RakshaQuant {report['date']} (session {report['sessions_to_date']})"]
    for b, data in report["books"].items():
        cap, pnl = data["capital"], data["pnl"]
        parts.append(f"{b}: ₹{_cell(cap['end_equity'])} ({_cell(pnl['day_return_pct'])}% day, "
                     f"{_cell(pnl['cumulative_return_pct'])}% total), trades {data['trades']['exits']}")  # fmt: skip
    for b, c in report["comparison"].items():
        parts.append(f"net AI value {b}−{c['vs']}: ₹{_cell(c['net_ai_value_inr'])}")
    alerts = report["infrastructure"]["alerts_by_level"]
    if alerts.get("CRITICAL"):
        parts.append(f"CRITICAL alerts: {alerts['CRITICAL']}")
    return "\n".join(parts)


async def send_summary(text: str, sender: Any, timeout_s: float = 5.0) -> bool:
    """Send with a hard timeout; never raises (the report is already on disk)."""
    try:
        return bool(await asyncio.wait_for(sender(text), timeout_s))
    except Exception as exc:
        logger.warning("daily report summary not sent: %s", type(exc).__name__)
        return False


def nifty_closes_from(series_dates: Iterable[date], closes: Iterable[float]) -> dict[date, float]:
    return dict(zip(series_dates, closes, strict=True))


def previous_session(calendar: NSECalendar, day: date) -> date | None:
    try:
        return calendar.previous_trading_day(day)
    except CalendarCoverageError:
        return day - timedelta(days=1)
