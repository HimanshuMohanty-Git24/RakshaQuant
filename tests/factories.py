"""Sample domain records and one sample payload per event type, shared by the v2 tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from src.domain import events as ev
from src.domain.base import EventPayload
from src.domain.types import (
    AdvisorKind,
    AdvisorVerdict,
    AnnouncementType,
    Bar,
    CheckLevel,
    CheckOutcome,
    EventDirection,
    Fill,
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    KillScope,
    KillSwitchState,
    MarketDataSource,
    Materiality,
    Order,
    OrderIntent,
    OrderStatus,
    Position,
    Product,
    Quote,
    ReasonCode,
    Regime,
    RiskCheckResult,
    RiskDecision,
    RiskOutcome,
    RiskSnapshotSummary,
    SessionState,
    Side,
    Signal,
    SignalReason,
    Timeframe,
    TypedEvent,
    Verdict,
    VerdictReason,
)

T0 = datetime(2026, 10, 5, 3, 50, tzinfo=UTC)  # 09:20 IST, Monday 5 Oct 2026
INFY = Instrument.nse_equity("INFY", isin="INE009A01021", sector="IT")


def intent(book_id: str = "A", decision_id: str = "d-0001", **overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "intent_id": f"{book_id}:momentum:{INFY.key}:2026-10-02:entry",
        "decision_id": decision_id,
        "book_id": book_id,
        "strategy": "momentum",
        "signal_id": "s-0001",
        "instrument": INFY,
        "side": Side.BUY,
        "kind": IntentKind.OPEN,
        "reduce_only": False,
        "decision_price": Decimal("1512.40"),
        "stop_price": Decimal("1471.20"),
        "target_price": Decimal("1574.20"),
        "decision_ts": T0,
        "reason": IntentReason.ENTRY,
        "source": IntentSource.SIGNAL_ENGINE,
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


def order(book_id: str = "A", client_order_id: str = "c0001", quantity: int = 13) -> Order:
    return Order(
        client_order_id=client_order_id,
        intent=intent(book_id),
        quantity=quantity,
        status=OrderStatus.SUBMITTED,
        submitted_at=T0,
    )


def fill(
    book_id: str = "A",
    fill_id: str = "f-0001",
    client_order_id: str = "c0001",
    quantity: int = 13,
    price: str = "1513.10",
    side: Side = Side.BUY,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        client_order_id=client_order_id,
        book_id=book_id,
        decision_id="d-0001",
        instrument_key=INFY.key,
        side=side,
        quantity=quantity,
        price=Decimal(price),
        ts=T0,
        charges=Decimal("23.81"),
        charges_breakdown={"stt": Decimal("19.67"), "stamp": Decimal("2.95")},
    )


def position(book_id: str = "A", quantity: int = 13, avg: str | None = "1513.10") -> Position:
    return Position(
        book_id=book_id,
        instrument_key=INFY.key,
        product=Product.CNC,
        quantity=quantity,
        avg_price=Decimal(avg) if avg is not None else None,
        realized_pnl=Decimal("0"),
        updated_at=T0,
    )


def risk_decision(book_id: str = "A", intent_id: str = "i-0001") -> RiskDecision:
    return RiskDecision(
        decision_id="d-0001",
        intent_id=intent_id,
        client_order_id="c0001",
        book_id=book_id,
        strategy="momentum",
        signal_id="s-0001",
        instrument_key=INFY.key,
        side=Side.BUY,
        product=Product.CNC,
        kind=IntentKind.OPEN,
        source=IntentSource.SIGNAL_ENGINE,
        qty_approved=13,
        ref_price=Decimal("1512.40"),
        outcome=RiskOutcome.RESIZED,
        notional=Decimal("19661.20"),
        risk_amount=Decimal("535.60"),
        risk_pct_equity=0.0536,
        reasons=(
            RiskCheckResult(
                code=ReasonCode.ORD_RISK_PER_TRADE,
                level=CheckLevel.ORDER,
                outcome=CheckOutcome.RESIZE,
                observed=20.0,
                limit=13.0,
                max_qty=13,
            ),
        ),
        checks_run=("ORD_RISK_PER_TRADE", "PF_MAX_POSITIONS"),
        limits_hash="9f2c",
        snapshot=RiskSnapshotSummary(
            equity=Decimal("1000000"),
            sod_equity=Decimal("1000000"),
            day_pnl_mtm=Decimal("0"),
            peak_equity=Decimal("1000000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            heat=Decimal("0"),
            open_positions=0,
            open_orders=0,
            quote_age_s=12.5,
            data_source=MarketDataSource.YFINANCE,
        ),
        engine_version="risk-1",
        evaluated_at=T0,
    )


def quote() -> Quote:
    return Quote(
        instrument_key=INFY.key,
        ltp=1512.4,
        prev_close=1498.0,
        volume_cum=120_000,
        exchange_ts=T0,
        receipt_ts=T0,
        source=MarketDataSource.YFINANCE,
        is_delayed=True,
    )


def sample_payloads() -> list[EventPayload]:
    """One valid payload for every registered event type."""
    ref = {
        "client_order_id": "c0001",
        "book_id": "A",
        "decision_id": "d-0001",
        "instrument_key": INFY.key,
    }
    return [
        ev.QuoteReceived(quote=quote()),
        ev.QuoteRejected(
            instrument_key=INFY.key,
            source=MarketDataSource.YFINANCE,
            reason="out_of_band",
            raw={"ltp": 9999.0},
        ),
        ev.BarClosed(
            bar=Bar(
                instrument_key=INFY.key,
                timeframe=Timeframe.D1,
                session_date=date(2026, 10, 1),
                open=1490.0,
                high=1520.0,
                low=1485.0,
                close=1498.0,
                volume=4_200_000,
                is_settled=True,
                source=MarketDataSource.YFINANCE,
            )
        ),
        ev.DataSourceChanged(current=MarketDataSource.YFINANCE, reason="startup"),
        ev.FeedStale(source=MarketDataSource.YFINANCE, instrument_keys=(INFY.key,), age_s=1300),
        ev.FeedRecovered(source=MarketDataSource.YFINANCE, stale_for_s=95.0),
        ev.SessionStateChanged(
            session_date=date(2026, 10, 5),
            previous=SessionState.OPEN,
            current=SessionState.ENTRY_WINDOW,
        ),
        ev.HolidaySkipped(session_date=date(2026, 10, 2), reason="Mahatma Gandhi Jayanti"),
        ev.RegimeComputed(
            session_date=date(2026, 10, 5),
            bar_date=date(2026, 10, 1),
            label=Regime.TRENDING_UP,
            raw=Regime.RANGING,
            changed=False,
            adx=27.5,
            plus_di=24.0,
            minus_di=15.0,
            vol_annualized=0.13,
            vol_percentile=0.42,
        ),
        ev.SignalGenerated(
            signal=Signal(
                signal_id="s-0001",
                decision_id="d-0001",
                instrument_key=INFY.key,
                strategy="momentum",
                side=Side.BUY,
                bar_date=date(2026, 10, 1),
                agreement_score=0.75,
                stop_atr_mult=2.0,
                target_atr_mult=3.0,
                reasons=(SignalReason(name="rsi_14", value=58.1),),
                generated_at=T0,
            )
        ),
        ev.AdvisorRequested(decision_id="d-0001", book_id="C", advisor=AdvisorKind.LLM_VETO),
        AdvisorVerdict(
            decision_id="d-0001",
            book_id="C",
            advisor=AdvisorKind.LLM_VETO,
            verdict=Verdict.VETO,
            confidence=0.7,
            reasons=(VerdictReason(claim="results tomorrow", evidence_ref="events[0]"),),
            provider="groq",
            model="llama-3.3-70b-versatile",
            latency_ms=812.0,
        ),
        ev.AdvisorFallback(
            decision_id="d-0001", book_id="B", advisor=AdvisorKind.TYPED_VETO, reason="timeout"
        ),
        ev.OrderIntentProposed(intent=intent()),
        risk_decision(),
        ev.KillSwitchChanged(
            book_id="A",
            scope=KillScope.GLOBAL,
            name="global",
            previous=KillSwitchState.ARMED,
            current=KillSwitchState.HALT_NEW,
            reason="daily loss",
            actor="monitor",
        ),
        ev.LimitBreached(
            book_id="A", code=ReasonCode.PF_DAILY_LOSS_MTM, observed=-10500.0, limit=-10000.0
        ),
        ev.DailyRiskStateRolled(
            book_id="A", ist_date=date(2026, 10, 5), state={"sod_equity": "1000000"}
        ),
        ev.OrderSubmitted(order=order()),
        ev.OrderAcked(**ref, broker_order_id="SIM-1", status=OrderStatus.OPEN),
        ev.OrderRejected(**ref, reason="PRICE_BAND"),
        ev.OrderUnknown(**ref, error="timeout"),
        ev.OrderCancelled(**ref, filled_qty=4, reason="participation cap"),
        ev.OrderExpired(**ref),
        ev.FillReceived(
            fill=fill(),
            order_status=OrderStatus.FILLED,
            order_filled_qty=13,
            order_avg_price=Decimal("1513.10"),
        ),
        ev.PositionChanged(position=position(), fill_id="f-0001"),
        ev.TradeClosed(
            trade_id="t-0001",
            book_id="A",
            decision_id="d-0001",
            instrument_key=INFY.key,
            strategy="momentum",
            side=Side.BUY,
            quantity=13,
            entry_price=Decimal("1513.10"),
            exit_price=Decimal("1560.00"),
            entry_ts=T0,
            exit_ts=T0,
            gross_pnl=Decimal("609.70"),
            charges=Decimal("47.90"),
            net_pnl=Decimal("561.80"),
            exit_reason="target",
        ),
        ev.MarkToMarket(
            book_id="A",
            equity=Decimal("1000561.80"),
            cash=Decimal("1000561.80"),
            positions_value=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            day_pnl=Decimal("561.80"),
        ),
        ev.ReconciliationResult(book_id="A", scope="oms_broker", in_sync=True),
        TypedEvent(
            event_id="nse-INFY-1",
            instrument_key=INFY.key,
            published_at=T0,
            title="Board meeting to consider results",
            source="nse_rss",
            relevant=True,
            announcement_type=AnnouncementType.RESULTS_DATE,
            direction=EventDirection.NEUTRAL,
            materiality=Materiality.MODERATE,
            probabilities={"relevant": {"yes": 0.93, "no": 0.07}},
            model="laya",
            calibrated=True,
            classified_at=T0,
        ),
        ev.LLMCall(
            decision_id="d-0001",
            book_id="C",
            role="veto",
            provider="groq",
            model="llama-3.3-70b-versatile",
            prompt_version="veto_v1",
            prompt_sha="ab12",
            tokens_in=812,
            tokens_out=96,
            latency_ms=640.0,
            cost_usd=Decimal("0"),
            cost_inr=Decimal("0"),
            outcome=ev.LLMOutcome.OK,
        ),
        ev.DecisionModelCall(
            decision_id="d-0001",
            book_id="B",
            task="typed_veto",
            model="laya",
            checkpoint="convaiinnovations/laya-typed-decisions",
            device="cpu",
            question_count=2,
            latency_ms=212.0,
            calibrated=True,
            answers={"veto": {"value": "no", "p": 0.81}},
            outcome=ev.LLMOutcome.OK,
        ),
        ev.BudgetThresholdCrossed(
            scope="total",
            threshold_pct=80.0,
            spent_inr=Decimal("40.10"),
            budget_inr=Decimal("50"),
        ),
        ev.Heartbeat(pid=4242, uptime_s=30.0, loop_lag_ms=3.1),
        ev.LoopLag(lag_ms=120.0, threshold_ms=50.0),
        ev.Alert(level="WARNING", key="feed_stale", message="YFinance stale for 20 min"),
        ev.ProcessStarted(
            pid=4242,
            entry_point="run_live_trading",
            argv=("--mode", "cli"),
            environment="test",
            version="0.1.0",
        ),
        ev.ProcessStopped(
            pid=4242, entry_point="run_live_trading", reason="normal", exit_code=0, uptime_s=60.0
        ),
    ]
