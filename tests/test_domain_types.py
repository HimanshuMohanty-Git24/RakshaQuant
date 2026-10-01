"""Plan M1.1: canonical domain types validate their invariants and round-trip through JSON."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.domain.types import (
    AdvisorKind,
    AdvisorVerdict,
    Bar,
    CheckLevel,
    CheckOutcome,
    Fill,
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    MarketDataSource,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    Product,
    Quote,
    ReasonCode,
    RiskCheckResult,
    RiskDecision,
    RiskOutcome,
    RiskSnapshotSummary,
    Side,
    Signal,
    SignalReason,
    Timeframe,
    Verdict,
)

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 09:30 IST
INFY = Instrument.nse_equity("INFY", isin="INE009A01021", sector="IT")


def _intent(**overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "intent_id": "A:momentum:NSE:EQ:INFY:2026-10-02:entry",
        "decision_id": "d1",
        "book_id": "A",
        "strategy": "momentum",
        "instrument": INFY,
        "side": Side.BUY,
        "kind": IntentKind.OPEN,
        "reduce_only": False,
        "decision_price": Decimal("1512.40"),
        "decision_ts": NOW,
        "reason": IntentReason.ENTRY,
        "source": IntentSource.SIGNAL_ENGINE,
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


def _snapshot() -> RiskSnapshotSummary:
    return RiskSnapshotSummary(
        equity=Decimal("1000000"),
        sod_equity=Decimal("1000000"),
        day_pnl_mtm=Decimal("0"),
        peak_equity=Decimal("1000000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        heat=Decimal("0"),
        open_positions=0,
        open_orders=0,
    )


def _decision(**overrides: object) -> RiskDecision:
    fields: dict[str, object] = {
        "decision_id": "d1",
        "intent_id": "i1",
        "book_id": "A",
        "strategy": "momentum",
        "instrument_key": INFY.key,
        "side": Side.BUY,
        "product": Product.CNC,
        "kind": IntentKind.OPEN,
        "source": IntentSource.SIGNAL_ENGINE,
        "qty_approved": 13,
        "outcome": RiskOutcome.APPROVED,
        "notional": Decimal("19661.20"),
        "risk_amount": Decimal("1000.00"),
        "risk_pct_equity": 0.001,
        "limits_hash": "abc123",
        "snapshot": _snapshot(),
        "engine_version": "risk-1",
        "evaluated_at": NOW,
    }
    fields.update(overrides)
    return RiskDecision.model_validate(fields)


# --- Instrument ---------------------------------------------------------------------------


def test_instrument_key_is_derived_and_checked():
    assert INFY.key == "NSE:EQ:INFY"
    assert INFY.tick_size == Decimal("0.05")
    with pytest.raises(ValidationError, match="must be"):
        Instrument(key="NSE:EQ:TCS", exchange="NSE", segment="CM", symbol="INFY", series="EQ")


def test_instrument_is_hashable_with_value_equality_and_frozen():
    same = Instrument.nse_equity("INFY", isin="INE009A01021", sector="IT")
    assert {INFY, same} == {INFY}
    stale = Instrument.nse_equity("INFY")  # same key, different reference data: distinct
    assert stale != INFY and hash(stale) == hash(INFY)
    with pytest.raises(ValidationError):
        INFY.symbol = "TCS"  # type: ignore[misc]


# --- Market data ----------------------------------------------------------------------------


@pytest.mark.parametrize("ltp", [0.0, -1.0, float("nan"), float("inf")])
def test_quote_rejects_non_positive_or_non_finite_prices(ltp):
    with pytest.raises(ValidationError):
        Quote(
            instrument_key=INFY.key,
            ltp=ltp,
            exchange_ts=NOW,
            receipt_ts=NOW,
            source=MarketDataSource.YFINANCE,
        )


def test_quote_requires_aware_timestamps():
    with pytest.raises(ValidationError):
        Quote(
            instrument_key=INFY.key,
            ltp=1500.0,
            exchange_ts=datetime(2026, 10, 5, 9, 30),
            receipt_ts=NOW,
            source=MarketDataSource.YFINANCE,
        )


def test_quote_age_uses_exchange_time():
    q = Quote(
        instrument_key=INFY.key,
        ltp=1500.0,
        exchange_ts=NOW - timedelta(minutes=15),
        receipt_ts=NOW,
        source=MarketDataSource.YFINANCE,
        is_delayed=True,
    )
    assert q.age_seconds(NOW) == 900.0


def test_fabricated_sources_are_flagged():
    assert MarketDataSource.SIMULATED.is_fabricated
    assert MarketDataSource.SYNTHETIC.is_fabricated
    assert not MarketDataSource.YFINANCE.is_fabricated
    assert not MarketDataSource.REPLAY.is_fabricated


def test_bar_rejects_inconsistent_ohlc():
    common = {
        "instrument_key": INFY.key,
        "timeframe": Timeframe.D1,
        "session_date": date(2026, 10, 1),
        "is_settled": True,
        "source": MarketDataSource.YFINANCE,
    }
    Bar(open=100, high=110, low=95, close=105, **common)
    with pytest.raises(ValidationError, match="inconsistent OHLC"):
        Bar(open=100, high=99, low=95, close=105, **common)
    with pytest.raises(ValidationError, match="inconsistent OHLC"):
        Bar(open=100, high=110, low=101, close=105, **common)


def test_signal_reason_rejects_non_finite_values():
    with pytest.raises(ValidationError, match="non-finite"):
        SignalReason(name="rsi_14", value=float("nan"))


def test_signal_round_trips():
    s = Signal(
        signal_id="s1",
        decision_id="d1",
        instrument_key=INFY.key,
        strategy="mean_reversion",
        side=Side.BUY,
        bar_date=date(2026, 10, 1),
        agreement_score=0.75,
        stop_atr_mult=2.0,
        target_atr_mult=3.0,
        reasons=(SignalReason(name="rsi_14", value=28.4, detail="oversold"),),
        generated_at=NOW,
    )
    assert Signal.model_validate_json(s.model_dump_json()) == s


# --- Orders -----------------------------------------------------------------------------------


def test_intent_reduce_only_must_match_kind():
    _intent()
    _intent(kind=IntentKind.CLOSE, reduce_only=True, side=Side.SELL, reason=IntentReason.STOP)
    with pytest.raises(ValidationError, match="reduce_only"):
        _intent(kind=IntentKind.CLOSE, reduce_only=False)
    with pytest.raises(ValidationError, match="reduce_only"):
        _intent(kind=IntentKind.OPEN, reduce_only=True)


def test_intent_order_type_needs_its_prices():
    with pytest.raises(ValidationError, match="limit_price"):
        _intent(order_type=OrderType.LIMIT)
    with pytest.raises(ValidationError, match="trigger_price"):
        _intent(order_type=OrderType.SL_M)
    _intent(order_type=OrderType.SL_M, trigger_price=Decimal("1470.00"))


def test_intent_is_unsized_until_risk_sizes_it():
    assert _intent().quantity is None
    with pytest.raises(ValidationError):
        _intent(quantity=0)


def test_order_fill_bounds_and_remaining():
    order = Order(client_order_id="c1", intent=_intent(), quantity=10)
    assert order.status is OrderStatus.PENDING_NEW and order.remaining_qty == 10
    partial = order.model_copy(
        update={
            "status": OrderStatus.PARTIALLY_FILLED,
            "filled_qty": 4,
            "avg_fill_price": Decimal("1513"),
        }
    )
    assert partial.remaining_qty == 6
    with pytest.raises(ValidationError, match="exceeds"):
        Order(client_order_id="c1", intent=_intent(), quantity=10, filled_qty=11)
    with pytest.raises(ValidationError, match="avg_fill_price"):
        Order(client_order_id="c1", intent=_intent(), quantity=10, filled_qty=4)


def test_terminal_statuses():
    terminal = {s for s in OrderStatus if s.is_terminal}
    assert terminal == {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }


def test_fill_keeps_exact_decimals_through_json():
    fill = Fill(
        fill_id="f1",
        client_order_id="c1",
        book_id="A",
        decision_id="d1",
        instrument_key=INFY.key,
        side=Side.BUY,
        quantity=13,
        price=Decimal("1512.45"),
        ts=NOW,
        charges=Decimal("23.81"),
        charges_breakdown={"stt": Decimal("19.66"), "stamp": Decimal("2.95")},
    )
    data = fill.model_dump(mode="json")
    assert data["price"] == "1512.45"
    back = Fill.model_validate(data)
    assert back == fill and back.notional == Decimal("19661.85")


def test_position_avg_price_iff_open():
    Position(book_id="A", instrument_key=INFY.key, product=Product.CNC, quantity=0, updated_at=NOW)
    Position(
        book_id="A",
        instrument_key=INFY.key,
        product=Product.CNC,
        quantity=10,
        avg_price=Decimal("1500"),
        updated_at=NOW,
    )
    with pytest.raises(ValidationError, match="avg_price"):
        Position(
            book_id="A", instrument_key=INFY.key, product=Product.CNC, quantity=5, updated_at=NOW
        )


def test_side_helpers():
    assert Side.BUY.sign == 1 and Side.SELL.sign == -1
    assert Side.BUY.opposite is Side.SELL


# --- Risk ---------------------------------------------------------------------------------------


def test_every_reason_code_has_a_level():
    for code in ReasonCode:
        assert isinstance(code.level, CheckLevel)
    assert ReasonCode.SYS_HOLIDAY.level is CheckLevel.SYSTEM
    assert ReasonCode.EVT_RESULTS_WINDOW.level is CheckLevel.ORDER


def test_check_result_level_and_resize_qty_are_consistent():
    RiskCheckResult(
        code=ReasonCode.ORD_RISK_PER_TRADE,
        level=CheckLevel.ORDER,
        outcome=CheckOutcome.RESIZE,
        max_qty=13,
    )
    with pytest.raises(ValidationError, match="level"):
        RiskCheckResult(
            code=ReasonCode.SYS_HOLIDAY, level=CheckLevel.ORDER, outcome=CheckOutcome.BLOCK
        )
    with pytest.raises(ValidationError, match="max_qty"):
        RiskCheckResult(
            code=ReasonCode.ORD_ADV_PCT, level=CheckLevel.ORDER, outcome=CheckOutcome.RESIZE
        )


def test_risk_decision_outcome_must_match_quantity():
    _decision()
    blocked = RiskCheckResult(
        code=ReasonCode.SYS_SESSION_CLOSED, level=CheckLevel.SYSTEM, outcome=CheckOutcome.BLOCK
    )
    _decision(outcome=RiskOutcome.REJECTED, qty_approved=0, reasons=(blocked,))
    with pytest.raises(ValidationError, match="inconsistent"):
        _decision(outcome=RiskOutcome.APPROVED, qty_approved=0)
    with pytest.raises(ValidationError, match="reasons"):
        _decision(outcome=RiskOutcome.REJECTED, qty_approved=0)


def test_risk_decision_round_trips_and_is_an_event_payload():
    d = _decision()
    assert RiskDecision.event_type == "RiskDecision"
    assert RiskDecision.model_validate_json(d.model_dump_json()) == d


# --- Advisors -------------------------------------------------------------------------------------


def test_abstain_must_say_why():
    AdvisorVerdict(
        decision_id="d1",
        book_id="C",
        advisor=AdvisorKind.LLM_VETO,
        verdict=Verdict.ABSTAIN,
        abstain_reason="timeout",
    )
    with pytest.raises(ValidationError, match="abstain_reason"):
        AdvisorVerdict(
            decision_id="d1", book_id="C", advisor=AdvisorKind.LLM_VETO, verdict=Verdict.ABSTAIN
        )
    with pytest.raises(ValidationError):
        AdvisorVerdict(
            decision_id="d1",
            book_id="C",
            advisor=AdvisorKind.LLM_VETO,
            verdict=Verdict.VETO,
            confidence=1.5,
        )
