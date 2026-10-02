"""Plan M4.1: the RiskEngine - one test per reason code, the audit probes, batch reservations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.config.limits import RiskLimits, load_risk_limits
from src.domain.types import (
    CheckOutcome,
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    KillState,
    KillSwitchState,
    MarketDataSource,
    OrderIntent,
    OrderType,
    Quote,
    ReasonCode,
    RiskOutcome,
    Side,
)
from src.risk.checks.base import ALL_KINDS, Check
from src.risk.engine import RiskEngine
from src.risk.events import EventBlock
from src.risk.snapshot import MarketFacts, PositionInfo, RiskSnapshot, StrategyStats

R = ReasonCode
NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 09:30 IST
INFY = Instrument.nse_equity("INFY", sector="IT")
LAKH = Decimal(100_000)


def quote(
    ltp: float = 1000.0, *, prev: float = 990.0, age_s: float = 900, key: str = INFY.key
) -> Quote:
    return Quote(
        instrument_key=key,
        ltp=ltp,
        prev_close=prev,
        volume_cum=1_000_000,
        exchange_ts=NOW - timedelta(seconds=age_s),
        receipt_ts=NOW,
        source=MarketDataSource.YFINANCE,
        is_delayed=True,
    )


def facts(**kw: Any) -> MarketFacts:
    base: dict[str, Any] = {
        "quote": quote(),
        "atr": Decimal(20),
        "adv_shares": 5_000_000.0,
        "data_source": MarketDataSource.YFINANCE,
    }
    base.update(kw)
    return MarketFacts(**base)


def snapshot(**kw: Any) -> RiskSnapshot:
    base: dict[str, Any] = {
        "now": NOW,
        "environment": "paper",
        "equity": Decimal(1_000_000),
        "cash": Decimal(1_000_000),
        "sod_equity": Decimal(1_000_000),
        "peak_equity": Decimal(1_000_000),
        "market": {INFY.key: facts()},
    }
    base.update(kw)
    return RiskSnapshot(**base)


def intent(**kw: Any) -> OrderIntent:
    side = kw.pop("side", Side.BUY)
    kind = kw.pop("kind", IntentKind.OPEN)
    base: dict[str, Any] = {
        "intent_id": f"A:momentum:{INFY.key}:2026-10-02:entry",
        "decision_id": "d1",
        "book_id": "A",
        "strategy": "momentum",
        "instrument": INFY,
        "side": side,
        "kind": kind,
        "reduce_only": kind.reduces_risk,
        "decision_price": Decimal(1000),
        "stop_price": Decimal(960),  # 2 ATR, 4%
        "target_price": Decimal(1060),  # R:R 1.5
        "decision_ts": NOW,
        "reason": IntentReason.ENTRY if not kind.reduces_risk else IntentReason.STOP,
        "source": IntentSource.SIGNAL_ENGINE
        if not kind.reduces_risk
        else IntentSource.EXIT_MANAGER,
    }
    base.update(kw)
    return OrderIntent(**base)


def other(symbol: str, notional: Decimal, *, sector: str, strategy: str = "momentum",
          stop: Decimal | None = None) -> PositionInfo:  # fmt: skip
    return PositionInfo(
        instrument_key=f"NSE:EQ:{symbol}", quantity=int(notional / 100), avg_price=Decimal(100),
        mark=Decimal(100), sector=sector, strategy=strategy, stop_price=stop,
    )  # fmt: skip


def engine(**limits: Any) -> RiskEngine:
    return RiskEngine(load_risk_limits(limits) if limits else load_risk_limits())


def test_healthy_baseline_is_sized_by_the_notional_cap():
    ev = engine().evaluate(intent(), snapshot())
    d = ev.decision
    assert d.outcome is RiskOutcome.APPROVED and d.qty_approved == 100  # 10% of 10L at 1,000
    assert d.risk_amount == Decimal(4000) and d.risk_pct_equity == pytest.approx(0.004)
    assert d.limits_hash == RiskLimits().limits_hash() and d.engine_version == "risk-1"
    assert d.client_order_id and "ORD_RISK_PER_TRADE" in d.checks_run


# --- one case per reason code --------------------------------------------------------------------

Case = tuple[
    ReasonCode, CheckOutcome, Callable[[], tuple[RiskEngine, OrderIntent, RiskSnapshot, int | None]]
]


def _c(code: ReasonCode, outcome: CheckOutcome, eng: RiskEngine | None = None,
       i: OrderIntent | None = None, s: RiskSnapshot | None = None, qty: int | None = None) -> Case:  # fmt: skip
    return (code, outcome, lambda: (eng or engine(), i or intent(), s or snapshot(), qty))


B, RS, A = CheckOutcome.BLOCK, CheckOutcome.RESIZE, CheckOutcome.ALLOW
held_infy = {INFY.key: PositionInfo(INFY.key, 30, Decimal(990), Decimal(1000), "IT", "momentum")}
CASES: list[Case] = [
    _c(R.ORD_QTY_NONPOS, B, qty=0),
    _c(R.ORD_NOTIONAL_MAX, RS, qty=500),
    _c(R.ORD_RISK_PER_TRADE, RS, eng=engine(risk_per_trade=0.001)),
    _c(R.ORD_STOP_WRONG_SIDE, B, i=intent(stop_price=Decimal(1001))),
    _c(R.ORD_STOP_TOO_WIDE, B, i=intent(stop_price=Decimal(900), target_price=Decimal(1200))),
    _c(R.ORD_STOP_TOO_TIGHT, B, i=intent(stop_price=Decimal(995))),
    _c(R.ORD_RR_MIN, B, i=intent(target_price=Decimal(1040))),
    _c(R.ORD_PRICE_COLLAR, B, i=intent(decision_price=Decimal(1100))),
    _c(R.ORD_ADV_PCT, RS, s=snapshot(market={INFY.key: facts(adv_shares=2000.0)})),
    _c(R.ORD_CIRCUIT_BAND, B, i=intent(instrument=Instrument.nse_equity("INFY", sector="IT", band_pct=2.0)),
       s=snapshot(market={INFY.key: facts(quote=quote(prev=981.0))})),  # +1.9% vs a 2% band
    _c(R.ORD_TICK, B, i=intent(order_type=OrderType.SL_M, trigger_price=Decimal("1000.03"))),
    _c(R.ORD_SHORT_NOT_ALLOWED, B, i=intent(side=Side.SELL, stop_price=Decimal(1040), target_price=Decimal(940))),
    _c(R.ORD_REDUCE_EXCEEDS_POS, RS, i=intent(side=Side.SELL, kind=IntentKind.CLOSE, quantity=50), s=snapshot(positions=held_infy)),
    _c(R.STR_HALTED, B, s=snapshot(kill=KillState(strategies={"momentum": KillSwitchState.HALT_NEW}))),
    _c(R.STR_NOT_VALIDATED, B, i=intent(strategy="breakout")),
    _c(R.STR_CAPITAL_ALLOC, B, s=snapshot(positions={"k": other("X", Decimal(500_000), sector="Energy")}),
       eng=engine(max_gross_exposure_pct=1.0)),
    _c(R.STR_DAILY_LOSS, B, s=snapshot(strategies={"momentum": StrategyStats(day_pnl=Decimal(-6000))})),
    _c(R.STR_CONSEC_LOSSES, B, s=snapshot(strategies={"momentum": StrategyStats(consecutive_losses=4)})),
    _c(R.STR_ORDER_RATE, B, s=snapshot(strategies={"momentum": StrategyStats(orders_last_min=5)})),
    _c(R.PF_MAX_POSITIONS, B, s=snapshot(positions={f"k{n}": other(f"S{n}", Decimal(10_000), sector=f"s{n}") for n in range(5)})),
    _c(R.PF_DUPLICATE, B, s=snapshot(positions=held_infy)),
    _c(R.PF_REENTRY_SAME_DAY, B, s=snapshot(symbols_exited_today=frozenset({INFY.key}))),
    _c(R.PF_GROSS, B, s=snapshot(positions={"a": other("A", Decimal(250_000), sector="a", strategy="x"),
                                            "b": other("B", Decimal(250_000), sector="b", strategy="y")})),  # fmt: skip
    _c(R.PF_SECTOR, B, s=snapshot(positions={"a": other("A", Decimal(300_000), sector="IT", strategy="x")})),
    _c(R.PF_HEAT, B, s=snapshot(positions={"a": other("A", Decimal(60_000), sector="a", strategy="x")})),
    _c(R.PF_CASH, B, s=snapshot(cash=Decimal(0))),
    _c(R.PF_DAILY_LOSS_MTM, B, s=snapshot(equity=Decimal(989_000))),
    _c(R.PF_DRAWDOWN, B, s=snapshot(peak_equity=Decimal(1_060_000))),
    _c(R.PF_ENTRIES_PER_DAY, B, s=snapshot(entries_today=10)),
    _c(R.SYS_KILL_GLOBAL, B, s=snapshot(kill=KillState(global_state=KillSwitchState.HALT_NEW))),
    _c(R.SYS_KILL_BROKER, B, s=snapshot(kill=KillState(broker=KillSwitchState.FLATTEN))),
    _c(R.SYS_SESSION_CLOSED, B, s=snapshot(session_open=False)),
    _c(R.SYS_HOLIDAY, B, s=snapshot(is_trading_day=False)),
    _c(R.SYS_ENTRY_CUTOFF, B, s=snapshot(entry_window=False)),
    _c(R.SYS_DATA_STALE, B, s=snapshot(market={INFY.key: facts(quote=quote(age_s=1300))})),
    _c(R.SYS_DATA_SIMULATED, B, s=snapshot(market={INFY.key: facts(data_source=MarketDataSource.SIMULATED)})),
    _c(R.SYS_MAX_OPEN_ORDERS, B, s=snapshot(open_orders=20)),
    _c(R.SYS_ORDER_RATE, B, s=snapshot(orders_last_min=10)),
    _c(R.SYS_REJECT_STORM, B, s=snapshot(rejects_in_window=5)),
    _c(R.SYS_UNKNOWN_ORDER, B, s=snapshot(unknown_order_keys=frozenset({INFY.key}))),
    _c(R.SYS_LLM_DEGRADED, A, s=snapshot(llm_degraded=True)),
    _c(R.SYS_JOURNAL_NOT_DURABLE, B, s=snapshot(journal_durable=False)),
    _c(R.SYS_RECON_DRIFT, B, s=snapshot(recon_drift=True)),
    _c(R.EVT_RESULTS_WINDOW, B, s=snapshot(event_blocks={INFY.key: (
        EventBlock(R.EVT_RESULTS_WINDOW, date(2026, 10, 1), date(2026, 10, 6), "e1", "results"),)})),
    _c(R.EVT_ADVERSE_MAJOR, B, s=snapshot(event_blocks={INFY.key: (
        EventBlock(R.EVT_ADVERSE_MAJOR, date(2026, 10, 5), date(2026, 10, 7), "e2", "penalty"),)})),
]  # fmt: skip


@pytest.mark.parametrize(("code", "outcome", "build"), CASES, ids=[c[0].value for c in CASES])
def test_reason_code(code, outcome, build):
    eng, i, s, qty = build()
    d = eng.evaluate(i, s, quantity=qty).decision
    hits = [r for r in d.reasons if r.code is code]
    assert hits, f"{code} not reported: {[r.code.value for r in d.reasons]} ({d.outcome})"
    assert hits[0].outcome is outcome
    if outcome is B:
        assert d.qty_approved == 0 and d.outcome in (RiskOutcome.REJECTED, RiskOutcome.HALTED)
    elif outcome is RS:
        assert d.qty_approved > 0 and d.qty_approved == hits[0].max_qty
    else:
        assert d.outcome is RiskOutcome.APPROVED


def test_every_reason_code_is_covered():
    excluded = {R.PF_NET, R.SYS_CHECK_ERROR}
    assert {c[0] for c in CASES} == set(ReasonCode) - excluded  # SYS_CHECK_ERROR: below


def test_kill_switch_blocks_are_halted_not_rejected():
    d = (
        engine()
        .evaluate(intent(), snapshot(kill=KillState(global_state=KillSwitchState.HALT_NEW)))
        .decision
    )
    assert d.outcome is RiskOutcome.HALTED


def test_a_throwing_check_blocks_opens_and_allows_reductions():
    def boom(ctx: Any) -> None:
        raise RuntimeError("bug")

    eng = RiskEngine(RiskLimits(), checks=(Check(R.PF_GROSS, ALL_KINDS, boom),))
    opened = eng.evaluate(intent(), snapshot(), quantity=10).decision
    assert opened.outcome is RiskOutcome.REJECTED and opened.reasons[0].code is R.SYS_CHECK_ERROR
    closed = eng.evaluate(
        intent(side=Side.SELL, kind=IntentKind.CLOSE, quantity=10), snapshot()
    ).decision
    assert closed.outcome is RiskOutcome.APPROVED and closed.qty_approved == 10
    assert closed.reasons[0].code is R.SYS_CHECK_ERROR and closed.reasons[0].outcome is A


# --- the audit's probes ------------------------------------------------------------------------------


def test_probe_saturday_all_eight_signals_are_session_closed():
    saturday = snapshot(now=NOW + timedelta(days=5), is_trading_day=False, is_weekend=True,
                        session_open=False)  # fmt: skip
    symbols = ["INFY", "TCS", "ITC", "SBIN", "LT", "HDFCBANK", "RELIANCE", "AXISBANK"]
    decisions = [
        engine()
        .evaluate(
            intent(instrument=Instrument.nse_equity(s), intent_id=f"A:m:NSE:EQ:{s}:d:e"), saturday
        )
        .decision  # fmt: skip
        for s in symbols
    ]
    assert len(decisions) == 8
    for d in decisions:
        assert d.qty_approved == 0
        assert R.SYS_SESSION_CLOSED in {r.code for r in d.reasons}


def test_probe_9_9_percent_risk_is_resized_to_at_most_2_percent():
    """Audit A.4 #6: a stop edit let one trade risk 9.9% of capital against 2% configured."""
    hundred = {INFY.key: facts(quote=quote(100.0, prev=99.0), atr=Decimal("2.5"))}
    probe = intent(decision_price=Decimal(100), stop_price=Decimal(95), target_price=Decimal(110))
    d = engine().evaluate(probe, snapshot(market=hundred), quantity=19_800).decision  # 9.9% risk
    assert d.outcome is RiskOutcome.RESIZED and d.qty_approved < 19_800
    assert d.risk_pct_equity <= 0.02
    # The audit's extreme form (a Rs 1 stop on a Rs 100 entry) is blocked outright.
    wild = intent(decision_price=Decimal(100), stop_price=Decimal(1), target_price=Decimal(400))
    blocked = engine().evaluate(wild, snapshot(market=hundred), quantity=1000).decision
    assert blocked.qty_approved == 0 and R.ORD_STOP_TOO_WIDE in {r.code for r in blocked.reasons}


def test_probe_wrong_side_stop_is_blocked():
    """Audit F-12: a stop at 101 on a BUY at 100 used to be approved."""
    hundred = {INFY.key: facts(quote=quote(100.0, prev=99.0), atr=Decimal(1))}
    d = (
        engine()
        .evaluate(
            intent(decision_price=Decimal(100), stop_price=Decimal(101), target_price=Decimal(103)),
            snapshot(market=hundred),
        )
        .decision
    )
    assert d.qty_approved == 0 and R.ORD_STOP_WRONG_SIDE in {r.code for r in d.reasons}


# --- batches and exits ------------------------------------------------------------------------------


def _three(eng: RiskEngine) -> list[Any]:
    syms = [("AAA", "s1"), ("BBB", "s2"), ("CCC", "s3")]
    market = {f"NSE:EQ:{s}": facts(quote=quote(key=f"NSE:EQ:{s}")) for s, _ in syms}
    intents = [
        intent(instrument=Instrument.nse_equity(s, sector=sec), intent_id=f"A:m:NSE:EQ:{s}:d:e")
        for s, sec in syms
    ]
    return eng.evaluate_batch(intents, snapshot(market=market))


def test_batch_reserves_capacity_between_proposals():
    out = _three(engine(max_gross_exposure_pct=0.25))
    assert [e.approved_qty for e in out] == [100, 100, 50]  # 250k gross shared, not 3 x 100k
    capped = engine(max_positions=2)
    out = _three(capped)
    assert [e.approved_qty for e in out] == [100, 100, 0]
    assert R.PF_MAX_POSITIONS in {r.code for r in out[2].decision.reasons}


def test_exits_are_not_blocked_by_entry_rules_or_kill_switches():
    stressed = snapshot(
        positions=held_infy,
        kill=KillState(global_state=KillSwitchState.FLATTEN),
        entry_window=False,
        market={INFY.key: facts(quote=quote(age_s=5000))},
        equity=Decimal(900_000),
    )
    d = (
        engine()
        .evaluate(intent(side=Side.SELL, kind=IntentKind.CLOSE, quantity=30), stressed)
        .decision
    )
    assert d.outcome is RiskOutcome.APPROVED and d.qty_approved == 30


def test_exits_still_cannot_trade_a_closed_market():
    d = (
        engine()
        .evaluate(
            intent(side=Side.SELL, kind=IntentKind.CLOSE, quantity=30),
            snapshot(positions=held_infy, session_open=False),
        )
        .decision
    )
    assert d.qty_approved == 0 and R.SYS_SESSION_CLOSED in {r.code for r in d.reasons}


def test_lot_sizes_floor_the_approved_quantity():
    lots = Instrument.nse_equity("INFY", sector="IT", lot_size=7)
    d = engine().evaluate(intent(instrument=lots), snapshot()).decision
    assert d.qty_approved == 98  # 100 capped, floored to a multiple of 7


def test_preview_matches_the_binding_decision():
    eng = engine()
    assert eng.preview(intent(), snapshot()) == eng.evaluate(intent(), snapshot()).decision


def test_snapshot_is_not_mutated():
    s = snapshot()
    before = replace(s)
    engine().evaluate_batch([intent()], s)
    assert s == before
