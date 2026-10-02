"""
Event envelope and catalogue (plan M1.2; audit §G.2).

Every fact the system learns or decides is an :class:`Event`: an envelope
``{seq, ts_utc, ist_date, type, schema_version, decision_id, cycle_id, book_id, symbol, source,
payload}`` around a typed payload. ``seq`` is assigned by the event store on append.

The envelope's ``decision_id``, ``book_id`` and ``symbol`` are derived from the payload (see
:meth:`EventPayload.routing`), so the two can never disagree; passing a contradicting value to
:func:`make_event` raises. ``symbol`` is the plain trading symbol (``INFY``), not the full
instrument key.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import AwareDatetime, Field, JsonValue

from src.domain.base import (
    DomainModel,
    EventPayload,
    Money,
    NonEmptyStr,
    NonNegFloat,
    NonNegInt,
    NonNegMoney,
    Price,
    Routing,
)
from src.domain.types import (
    AdvisorKind,
    AdvisorVerdict,
    Bar,
    Fill,
    KillScope,
    KillSwitchState,
    MarketDataSource,
    Order,
    OrderIntent,
    OrderStatus,
    Position,
    Quote,
    ReasonCode,
    Regime,
    RiskDecision,
    SessionState,
    Side,
    Signal,
    TypedEvent,
)
from src.utils.market_time import IST

# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    payload: EventPayload
    ts_utc: datetime
    ist_date: date
    source: str
    schema_version: int
    seq: int | None = None
    decision_id: str | None = None
    cycle_id: str | None = None
    book_id: str | None = None
    symbol: str | None = None

    def with_seq(self, seq: int) -> Event:
        return replace(self, seq=seq)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form (payload decimals become strings)."""
        return {
            "seq": self.seq,
            "ts_utc": self.ts_utc.isoformat(),
            "ist_date": self.ist_date.isoformat(),
            "type": self.type,
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "cycle_id": self.cycle_id,
            "book_id": self.book_id,
            "symbol": self.symbol,
            "source": self.source,
            "payload": self.payload.model_dump(mode="json"),
        }


def symbol_of(instrument_key: str) -> str:
    """``"NSE:EQ:INFY"`` -> ``"INFY"``."""
    return instrument_key.rsplit(":", 1)[-1]


def make_event(
    payload: EventPayload,
    *,
    ts: datetime,
    source: str,
    cycle_id: str | None = None,
    decision_id: str | None = None,
    book_id: str | None = None,
    symbol: str | None = None,
) -> Event:
    """Wrap ``payload`` in an envelope stamped at ``ts`` (must be timezone-aware)."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("event timestamps must be timezone-aware")
    if not source:
        raise ValueError("events need a source")
    routed = payload.routing()
    ts_utc = ts.astimezone(UTC)
    return Event(
        type=payload.event_type,
        payload=payload,
        ts_utc=ts_utc,
        ist_date=ts_utc.astimezone(IST).date(),
        source=source,
        schema_version=payload.schema_version,
        decision_id=_merge("decision_id", decision_id, routed.decision_id),
        cycle_id=cycle_id,
        book_id=_merge("book_id", book_id, routed.book_id),
        symbol=_merge(
            "symbol",
            symbol,
            symbol_of(routed.instrument_key) if routed.instrument_key else None,
        ),
    )


def _merge(name: str, explicit: str | None, derived: str | None) -> str | None:
    if explicit is not None and derived is not None and explicit != derived:
        raise ValueError(f"envelope {name}={explicit!r} contradicts the payload ({derived!r})")
    return explicit if explicit is not None else derived


# ---------------------------------------------------------------------------
# Payloads: market
# ---------------------------------------------------------------------------


class QuoteReceived(EventPayload):
    event_type = "QuoteReceived"
    quote: Quote

    def routing(self) -> Routing:
        return Routing(instrument_key=self.quote.instrument_key)


class QuoteRejected(EventPayload):
    """A quote failed validation and was dropped (plan M2.2)."""

    event_type = "QuoteRejected"
    instrument_key: NonEmptyStr
    source: MarketDataSource
    reason: NonEmptyStr
    raw: dict[str, JsonValue] = Field(default_factory=dict)


class BarClosed(EventPayload):
    event_type = "BarClosed"
    bar: Bar

    def routing(self) -> Routing:
        return Routing(instrument_key=self.bar.instrument_key)


class DataSourceChanged(EventPayload):
    event_type = "DataSourceChanged"
    previous: MarketDataSource | None = None
    current: MarketDataSource
    reason: str = ""


class FeedStale(EventPayload):
    event_type = "FeedStale"
    source: MarketDataSource
    instrument_keys: tuple[str, ...] = ()
    last_exchange_ts: AwareDatetime | None = None
    age_s: NonNegFloat | None = None
    reason: str = ""


class FeedRecovered(EventPayload):
    event_type = "FeedRecovered"
    source: MarketDataSource
    instrument_keys: tuple[str, ...] = ()
    stale_for_s: NonNegFloat | None = None


# ---------------------------------------------------------------------------
# Payloads: session
# ---------------------------------------------------------------------------


class SessionStateChanged(EventPayload):
    event_type = "SessionStateChanged"
    session_date: date
    previous: SessionState | None = None
    current: SessionState


class HolidaySkipped(EventPayload):
    event_type = "HolidaySkipped"
    session_date: date
    reason: str = ""


# ---------------------------------------------------------------------------
# Payloads: decision
# ---------------------------------------------------------------------------


class AnnouncementReceived(EventPayload):
    """A corporate announcement for a universe instrument (plan M7.6), point in time: the
    exchange's ``published_at`` and our ``received_at`` (what a replay may know, and when)."""

    event_type = "AnnouncementReceived"
    announcement_id: NonEmptyStr  # sha of (instrument, published_at, title)
    instrument_key: NonEmptyStr
    company: NonEmptyStr
    published_at: AwareDatetime
    received_at: AwareDatetime
    title: NonEmptyStr  # the announcement text
    subject: str = ""  # the exchange's category, e.g. "Financial Result Updates"
    url: str | None = None
    source: NonEmptyStr  # "nse_rss"


class AnnouncementCoverageGap(EventPayload):
    """Announcements between ``gap_from`` and ``gap_to`` may have been missed (feed outage, or
    more announcements than the feed window holds). Downstream gates treat it as unknown."""

    event_type = "AnnouncementCoverageGap"
    source: NonEmptyStr
    gap_from: AwareDatetime
    gap_to: AwareDatetime
    reason: NonEmptyStr


class RegimeComputed(EventPayload):
    """The day's NIFTY regime, computed pre-open from settled bars (plan M5.3)."""

    event_type = "RegimeComputed"
    session_date: date  # the trading day it applies to
    bar_date: date  # the last settled index bar it was computed from
    label: Regime  # after hysteresis
    raw: Regime  # the latest bar's own classification
    changed: bool  # the confirmed label changed on this bar
    adx: NonNegFloat | None = None
    plus_di: NonNegFloat | None = None
    minus_di: NonNegFloat | None = None
    vol_annualized: NonNegFloat | None = None
    vol_percentile: float | None = Field(default=None, ge=0, le=1)


class SignalGenerated(EventPayload):
    event_type = "SignalGenerated"
    signal: Signal

    def routing(self) -> Routing:
        return Routing(
            decision_id=self.signal.decision_id, instrument_key=self.signal.instrument_key
        )


class AdvisorRequested(EventPayload):
    event_type = "AdvisorRequested"
    decision_id: NonEmptyStr
    book_id: NonEmptyStr
    advisor: AdvisorKind
    signal_id: str | None = None
    model: str | None = None


class AdvisorFallback(EventPayload):
    """The advisor could not answer; the deterministic decision stands (``SYS_LLM_DEGRADED``)."""

    event_type = "AdvisorFallback"
    decision_id: NonEmptyStr
    book_id: NonEmptyStr
    advisor: AdvisorKind
    reason: NonEmptyStr
    error: str | None = None


class Disposition(StrEnum):
    """What one book did with one signal (plan M8.3)."""

    SHADOW_STRATEGY = "shadow_strategy"  # recorded only (plan D8)
    REGIME_GATED = "regime_gated"
    POLICY_SKIPPED = "policy_skipped"  # e.g. long-only, no ATR
    VETOED = "vetoed"  # the book's advisor
    RISK_REJECTED = "risk_rejected"  # the RiskEngine (or reduce-only/duplicate rules)
    SUBMITTED = "submitted"
    BROKER_REJECTED = "broker_rejected"
    UNKNOWN = "unknown"  # the order's outcome is not yet known


class SignalDisposition(EventPayload):
    event_type = "SignalDisposition"
    signal_id: NonEmptyStr
    decision_id: NonEmptyStr
    book_id: NonEmptyStr
    instrument_key: NonEmptyStr
    strategy: NonEmptyStr
    disposition: Disposition
    detail: str = ""
    client_order_id: str | None = None


class ShadowTradeOpened(EventPayload):
    """The counterfactual of a signal entered on the live tape (plan M8.3)."""

    event_type = "ShadowTradeOpened"
    signal_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument_key: NonEmptyStr
    strategy: NonEmptyStr
    is_shadow_strategy: bool
    entry_ts: AwareDatetime
    entry_price: Price
    quantity: NonNegInt
    stop_price: Price
    target_price: Price
    atr: Price


class ShadowTradeClosed(EventPayload):
    """A counterfactual settled at its exit, net of modelled costs (plan M8.3)."""

    event_type = "ShadowTradeClosed"
    signal_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument_key: NonEmptyStr
    strategy: NonEmptyStr
    is_shadow_strategy: bool
    entry_ts: AwareDatetime
    entry_price: Price
    exit_ts: AwareDatetime
    exit_price: Price
    exit_reason: NonEmptyStr  # stop, target, time
    quantity: NonNegInt
    gross_pnl: Money
    charges: NonNegMoney
    net_pnl: Money
    net_return_pct: float
    hold_sessions: NonNegInt


class ShadowAlphaSettled(EventPayload):
    """A counterfactual's excess return over NIFTY across its holding period (plan M8.3)."""

    event_type = "ShadowAlphaSettled"
    signal_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument_key: NonEmptyStr
    entry_date: date
    exit_date: date
    net_return_pct: float
    nifty_return_pct: float
    alpha_pct: float


class OrderIntentProposed(EventPayload):
    event_type = "OrderIntentProposed"
    intent: OrderIntent

    def routing(self) -> Routing:
        return Routing(
            decision_id=self.intent.decision_id,
            book_id=self.intent.book_id,
            instrument_key=self.intent.instrument.key,
        )


# ---------------------------------------------------------------------------
# Payloads: risk
# ---------------------------------------------------------------------------


class KillSwitchChanged(EventPayload):
    event_type = "KillSwitchChanged"
    book_id: NonEmptyStr
    scope: KillScope
    name: NonEmptyStr  # "global", a strategy name, or a broker name
    previous: KillSwitchState
    current: KillSwitchState
    reason: NonEmptyStr
    actor: NonEmptyStr  # "monitor", "halt_file", "api", "operator", ...


class LimitBreached(EventPayload):
    event_type = "LimitBreached"
    book_id: NonEmptyStr
    code: ReasonCode
    observed: float | str | None = None
    limit: float | str | None = None
    message: str = ""


class DailyRiskStateRolled(EventPayload):
    """A book's persisted per-IST-day risk state (fields defined by M4.3)."""

    event_type = "DailyRiskStateRolled"
    book_id: NonEmptyStr
    ist_date: date
    state: dict[str, JsonValue] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Payloads: orders
# ---------------------------------------------------------------------------


class _OrderRef(EventPayload):
    """Base for order-lifecycle updates that reference an order already in the store."""

    client_order_id: NonEmptyStr
    book_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument_key: NonEmptyStr


class OrderSubmitted(EventPayload):
    """Persisted *before* the broker call (audit §H.5)."""

    event_type = "OrderSubmitted"
    order: Order

    def routing(self) -> Routing:
        intent = self.order.intent
        return Routing(
            decision_id=intent.decision_id,
            book_id=intent.book_id,
            instrument_key=intent.instrument.key,
        )


class OrderAcked(_OrderRef):
    event_type = "OrderAcked"
    broker_order_id: NonEmptyStr
    status: OrderStatus


class OrderRejected(_OrderRef):
    event_type = "OrderRejected"
    reason: NonEmptyStr
    message: str = ""


class OrderUnknown(_OrderRef):
    event_type = "OrderUnknown"
    error: str = ""


class OrderCancelled(_OrderRef):
    event_type = "OrderCancelled"
    filled_qty: NonNegInt = 0
    reason: str = ""


class OrderExpired(_OrderRef):
    event_type = "OrderExpired"
    filled_qty: NonNegInt = 0


class FillReceived(EventPayload):
    """A fill, plus the order's cumulative state after applying it."""

    event_type = "FillReceived"
    fill: Fill
    order_status: OrderStatus
    order_filled_qty: NonNegInt
    order_avg_price: Price

    def routing(self) -> Routing:
        return Routing(
            decision_id=self.fill.decision_id,
            book_id=self.fill.book_id,
            instrument_key=self.fill.instrument_key,
        )


# ---------------------------------------------------------------------------
# Payloads: portfolio
# ---------------------------------------------------------------------------


class PositionChanged(EventPayload):
    """The position's full new state; only fills change positions (audit §H.5)."""

    event_type = "PositionChanged"
    position: Position
    fill_id: str | None = None

    def routing(self) -> Routing:
        return Routing(book_id=self.position.book_id, instrument_key=self.position.instrument_key)


class TradeClosed(EventPayload):
    """A closed round trip (or the closed part of one), net of both legs' charges."""

    event_type = "TradeClosed"
    trade_id: NonEmptyStr
    book_id: NonEmptyStr
    decision_id: NonEmptyStr
    exit_decision_id: str | None = None
    instrument_key: NonEmptyStr
    strategy: NonEmptyStr
    side: Side  # side of the opening leg
    quantity: NonNegInt
    entry_price: Price
    exit_price: Price
    entry_ts: AwareDatetime
    exit_ts: AwareDatetime
    gross_pnl: Money
    charges: NonNegMoney
    net_pnl: Money
    exit_reason: NonEmptyStr


class ReviewLesson(DomainModel):
    claim: NonEmptyStr
    evidence_ref: NonEmptyStr


class TradeReview(EventPayload):
    """The nightly review of one closed trade (plan M8.7), stored point in time. **Never fed back
    into any book during month 1** (audit F-20): no decision or risk code reads it."""

    event_type = "TradeReview"
    trade_id: NonEmptyStr
    book_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument_key: NonEmptyStr
    summary: str
    what_worked: tuple[str, ...] = ()
    what_failed: tuple[str, ...] = ()
    lessons: tuple[ReviewLesson, ...] = ()
    dropped_lessons: NonNegInt = 0  # lessons citing nothing in the input (hallucinations)
    model: NonEmptyStr
    prompt_version: NonEmptyStr
    resolved_at: AwareDatetime


class MarkToMarket(EventPayload):
    event_type = "MarkToMarket"
    book_id: NonEmptyStr
    equity: Money
    cash: Money
    positions_value: Money
    unrealized_pnl: Money
    day_pnl: Money


class ReconciliationResult(EventPayload):
    event_type = "ReconciliationResult"
    book_id: NonEmptyStr
    scope: NonEmptyStr  # "oms_broker", "book_exit_manager", ...
    in_sync: bool
    diffs: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Payloads: economics (AI calls and budgets)
# ---------------------------------------------------------------------------


class LLMOutcome(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    INVALID_OUTPUT = "invalid_output"
    REFUSAL = "refusal"
    BUDGET_EXCEEDED = "budget_exceeded"
    BREAKER_OPEN = "breaker_open"  # the model was skipped: its circuit breaker is open
    ERROR = "error"


class LLMCall(EventPayload):
    """One LLM attempt (plan M6 router): every fallback attempt is its own event."""

    event_type = "LLMCall"
    decision_id: str | None = None
    book_id: str | None = None
    role: NonEmptyStr
    provider: NonEmptyStr
    model: NonEmptyStr
    attempt: NonNegInt = 0
    prompt_version: NonEmptyStr
    prompt_sha: NonEmptyStr
    tokens_in: NonNegInt = 0
    tokens_out: NonNegInt = 0
    cache_read_tokens: NonNegInt = 0
    cache_write_tokens: NonNegInt = 0
    latency_ms: NonNegFloat
    cost_usd: NonNegMoney | None = None  # None = unknown price (flagged)
    cost_inr: NonNegMoney | None = None
    cache_hit: bool = False
    outcome: LLMOutcome
    error: str | None = None


class DecisionModelCall(EventPayload):
    """One typed-decision-model call (plan M7.3): Laya local, Jev remote, or a cascade step."""

    event_type = "DecisionModelCall"
    decision_id: str | None = None
    book_id: str | None = None
    task: NonEmptyStr  # "announcements", "typed_veto", ...
    model: NonEmptyStr  # "laya", "jev"
    checkpoint: NonEmptyStr
    device: str | None = None
    question_count: NonNegInt
    latency_ms: NonNegFloat
    escalated: bool = False
    escalation_reason: str | None = None
    shadow: bool = False  # sampled to Jev only to measure agreement
    calibrated: bool = False
    answers: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    outcome: LLMOutcome
    error: str | None = None


class BudgetThresholdCrossed(EventPayload):
    event_type = "BudgetThresholdCrossed"
    scope: NonEmptyStr  # "total", "role:veto", ...
    threshold_pct: NonNegFloat
    spent_inr: NonNegMoney
    budget_inr: NonNegMoney


# ---------------------------------------------------------------------------
# Payloads: operations
# ---------------------------------------------------------------------------


class Heartbeat(EventPayload):
    event_type = "Heartbeat"
    pid: NonNegInt
    uptime_s: NonNegFloat
    loop_lag_ms: NonNegFloat | None = None


class LoopLag(EventPayload):
    event_type = "LoopLag"
    lag_ms: NonNegFloat
    threshold_ms: NonNegFloat


class Alert(EventPayload):
    event_type = "Alert"
    level: NonEmptyStr  # "INFO", "WARNING", "CRITICAL"
    key: NonEmptyStr
    message: NonEmptyStr


class ProcessStarted(EventPayload):
    event_type = "ProcessStarted"
    pid: NonNegInt
    entry_point: NonEmptyStr
    argv: tuple[str, ...] = ()
    environment: NonEmptyStr
    version: NonEmptyStr


class ProcessStopped(EventPayload):
    event_type = "ProcessStopped"
    pid: NonNegInt
    entry_point: NonEmptyStr
    reason: NonEmptyStr  # "normal", "interrupted", "crash", "config_error", ...
    exit_code: int
    uptime_s: NonNegFloat


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PAYLOADS: tuple[type[EventPayload], ...] = (
    # market
    QuoteReceived,
    QuoteRejected,
    BarClosed,
    DataSourceChanged,
    FeedStale,
    FeedRecovered,
    # session
    SessionStateChanged,
    HolidaySkipped,
    # announcements
    AnnouncementReceived,
    AnnouncementCoverageGap,
    # decision
    RegimeComputed,
    SignalGenerated,
    AdvisorRequested,
    AdvisorVerdict,
    AdvisorFallback,
    OrderIntentProposed,
    SignalDisposition,
    ShadowTradeOpened,
    ShadowTradeClosed,
    ShadowAlphaSettled,
    # risk
    RiskDecision,
    KillSwitchChanged,
    LimitBreached,
    DailyRiskStateRolled,
    # orders
    OrderSubmitted,
    OrderAcked,
    OrderRejected,
    OrderUnknown,
    OrderCancelled,
    OrderExpired,
    FillReceived,
    # portfolio
    PositionChanged,
    TradeClosed,
    TradeReview,
    MarkToMarket,
    ReconciliationResult,
    # typed events, economics, ops
    TypedEvent,
    LLMCall,
    DecisionModelCall,
    BudgetThresholdCrossed,
    Heartbeat,
    LoopLag,
    Alert,
    ProcessStarted,
    ProcessStopped,
)

EVENT_TYPES: Mapping[str, type[EventPayload]] = MappingProxyType(
    {cls.event_type: cls for cls in _PAYLOADS}
)
if len(EVENT_TYPES) != len(_PAYLOADS):  # pragma: no cover - guards a copy-paste slip
    raise RuntimeError("duplicate event_type in the event catalogue")


class UnknownEventTypeError(LookupError):
    pass


class SchemaVersionError(ValueError):
    pass


def payload_class(event_type: str) -> type[EventPayload]:
    try:
        return EVENT_TYPES[event_type]
    except KeyError:
        raise UnknownEventTypeError(f"unknown event type {event_type!r}") from None


def load_payload(
    event_type: str, schema_version: int, data: str | Mapping[str, Any]
) -> EventPayload:
    """Rebuild a stored payload, refusing schema versions this code does not understand."""
    cls = payload_class(event_type)
    if schema_version != cls.schema_version:
        raise SchemaVersionError(
            f"{event_type} v{schema_version} stored, code reads v{cls.schema_version}"
        )
    if isinstance(data, str):
        return cls.model_validate_json(data)
    return cls.model_validate(dict(data))


def payload_json(payload: EventPayload) -> str:
    """Canonical JSON for storage: compact, keys sorted, decimals as strings."""
    return json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
