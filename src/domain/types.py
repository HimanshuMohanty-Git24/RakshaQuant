"""
Canonical domain types (plan M1.1; audit §H.2, §K.4, §L.3).

Conventions:

* Prices and money that reach the OMS, a broker or the cost model are ``Decimal``. Market data
  (``Quote``, ``Bar``) and features are ``float``.
* Every timestamp is timezone-aware.
* Records are frozen; derive a changed copy with ``model_copy(update=...)``.
* ``book_id`` names a paper book (A/B/C in month 1). ``decision_id`` is minted when a signal is
  generated and is shared by every book that sees it.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from src.domain.base import (
    DomainModel,
    EventPayload,
    Money,
    NonEmptyStr,
    NonNegFloat,
    NonNegInt,
    NonNegMoney,
    PosFloat,
    PosInt,
    Price,
    Probability,
)

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL_M"


class Product(StrEnum):
    MIS = "MIS"
    CNC = "CNC"
    NRML = "NRML"
    MTF = "MTF"


class Validity(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_MODIFY = "PENDING_MODIFY"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


class IntentKind(StrEnum):
    OPEN = "open"
    INCREASE = "increase"
    REDUCE = "reduce"
    CLOSE = "close"
    FLATTEN = "flatten"

    @property
    def reduces_risk(self) -> bool:
        return self in (IntentKind.REDUCE, IntentKind.CLOSE, IntentKind.FLATTEN)


class IntentReason(StrEnum):
    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    TRAIL = "trail"
    PARTIAL = "partial"
    TIME = "time"
    SQUARE_OFF = "square_off"
    FLATTEN = "flatten"
    OPERATOR = "operator"


class IntentSource(StrEnum):
    SIGNAL_ENGINE = "signal_engine"
    EXIT_MANAGER = "exit_manager"
    KILL_SWITCH = "kill_switch"
    OPERATOR = "operator"


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    D1 = "1d"


class MarketDataSource(StrEnum):
    YFINANCE = "yfinance"
    DHAN = "dhan"
    REPLAY = "replay"
    SIMULATED = "simulated"
    SYNTHETIC = "synthetic"

    @property
    def is_fabricated(self) -> bool:
        """Simulated or synthetic data must never create orders outside demo (plan M2.6)."""
        return self in (MarketDataSource.SIMULATED, MarketDataSource.SYNTHETIC)


class Regime(StrEnum):
    """The deterministic NIFTY market regime (plan M5.3): context, never an exit trigger."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"


class SessionState(StrEnum):
    """Session lifecycle states (plan M2.5)."""

    HOLIDAY = "HOLIDAY"
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    ENTRY_WINDOW = "ENTRY_WINDOW"
    MONITOR = "MONITOR"
    CLOSE = "CLOSE"
    REPORT = "REPORT"
    EXIT = "EXIT"


class KillSwitchState(StrEnum):
    ARMED = "ARMED"
    HALT_NEW = "HALT_NEW"
    FLATTEN = "FLATTEN"


class KillScope(StrEnum):
    GLOBAL = "global"
    STRATEGY = "strategy"
    BROKER = "broker"


class CheckLevel(StrEnum):
    ORDER = "order"
    STRATEGY = "strategy"
    PORTFOLIO = "portfolio"
    SYSTEM = "system"


class CheckOutcome(StrEnum):
    ALLOW = "allow"
    RESIZE = "resize"
    BLOCK = "block"


class RiskOutcome(StrEnum):
    APPROVED = "APPROVED"
    RESIZED = "RESIZED"
    REJECTED = "REJECTED"
    HALTED = "HALTED"


class ReasonCode(StrEnum):
    """Risk reason codes (audit §L.3, plus the M7.8 event-calendar gate)."""

    # Order
    ORD_QTY_NONPOS = "ORD_QTY_NONPOS"
    ORD_NOTIONAL_MAX = "ORD_NOTIONAL_MAX"
    ORD_RISK_PER_TRADE = "ORD_RISK_PER_TRADE"
    ORD_STOP_WRONG_SIDE = "ORD_STOP_WRONG_SIDE"
    ORD_STOP_TOO_WIDE = "ORD_STOP_TOO_WIDE"
    ORD_STOP_TOO_TIGHT = "ORD_STOP_TOO_TIGHT"
    ORD_RR_MIN = "ORD_RR_MIN"
    ORD_PRICE_COLLAR = "ORD_PRICE_COLLAR"
    ORD_ADV_PCT = "ORD_ADV_PCT"
    ORD_CIRCUIT_BAND = "ORD_CIRCUIT_BAND"
    ORD_TICK = "ORD_TICK"
    ORD_SHORT_NOT_ALLOWED = "ORD_SHORT_NOT_ALLOWED"
    ORD_REDUCE_EXCEEDS_POS = "ORD_REDUCE_EXCEEDS_POS"
    # Event-calendar gate (instrument-level, so it reports at order level)
    EVT_RESULTS_WINDOW = "EVT_RESULTS_WINDOW"
    EVT_ADVERSE_MAJOR = "EVT_ADVERSE_MAJOR"
    # Strategy
    STR_HALTED = "STR_HALTED"
    STR_NOT_VALIDATED = "STR_NOT_VALIDATED"
    STR_CAPITAL_ALLOC = "STR_CAPITAL_ALLOC"
    STR_DAILY_LOSS = "STR_DAILY_LOSS"
    STR_CONSEC_LOSSES = "STR_CONSEC_LOSSES"
    STR_ORDER_RATE = "STR_ORDER_RATE"
    # Portfolio
    PF_MAX_POSITIONS = "PF_MAX_POSITIONS"
    PF_DUPLICATE = "PF_DUPLICATE"
    PF_REENTRY_SAME_DAY = "PF_REENTRY_SAME_DAY"
    PF_GROSS = "PF_GROSS"
    PF_NET = "PF_NET"
    PF_SECTOR = "PF_SECTOR"
    PF_HEAT = "PF_HEAT"
    PF_CASH = "PF_CASH"
    PF_DAILY_LOSS_MTM = "PF_DAILY_LOSS_MTM"
    PF_DRAWDOWN = "PF_DRAWDOWN"
    PF_ENTRIES_PER_DAY = "PF_ENTRIES_PER_DAY"  # the legacy max-daily-trades limit
    # System
    SYS_KILL_GLOBAL = "SYS_KILL_GLOBAL"
    SYS_KILL_BROKER = "SYS_KILL_BROKER"
    SYS_SESSION_CLOSED = "SYS_SESSION_CLOSED"
    SYS_HOLIDAY = "SYS_HOLIDAY"
    SYS_ENTRY_CUTOFF = "SYS_ENTRY_CUTOFF"
    SYS_DATA_STALE = "SYS_DATA_STALE"
    SYS_DATA_SIMULATED = "SYS_DATA_SIMULATED"
    SYS_MAX_OPEN_ORDERS = "SYS_MAX_OPEN_ORDERS"
    SYS_ORDER_RATE = "SYS_ORDER_RATE"
    SYS_REJECT_STORM = "SYS_REJECT_STORM"
    SYS_UNKNOWN_ORDER = "SYS_UNKNOWN_ORDER"
    SYS_LLM_DEGRADED = "SYS_LLM_DEGRADED"
    SYS_JOURNAL_NOT_DURABLE = "SYS_JOURNAL_NOT_DURABLE"
    SYS_RECON_DRIFT = "SYS_RECON_DRIFT"
    SYS_CHECK_ERROR = "SYS_CHECK_ERROR"

    @property
    def level(self) -> CheckLevel:
        return _LEVEL_BY_PREFIX[self.value.split("_", 1)[0]]


_LEVEL_BY_PREFIX = {
    "ORD": CheckLevel.ORDER,
    "EVT": CheckLevel.ORDER,
    "STR": CheckLevel.STRATEGY,
    "PF": CheckLevel.PORTFOLIO,
    "SYS": CheckLevel.SYSTEM,
}


class AdvisorKind(StrEnum):
    NONE = "none"
    TYPED_VETO = "typed_veto"
    LLM_VETO = "llm_veto"


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    VETO = "VETO"
    ABSTAIN = "ABSTAIN"


class AnnouncementType(StrEnum):
    RESULTS = "results"
    RESULTS_DATE = "results_date"
    DIVIDEND = "dividend"
    SPLIT_BONUS = "split_bonus"
    PLEDGE = "pledge"
    INSIDER_OR_PROMOTER = "insider_or_promoter"
    ORDER_WIN = "order_win"
    LITIGATION_OR_REGULATORY = "litigation_or_regulatory"
    MANAGEMENT_CHANGE = "management_change"
    RATING_CHANGE = "rating_change"
    FUNDRAISE = "fundraise"
    OTHER = "other"


class EventDirection(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"


class Materiality(StrEnum):
    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"


# ---------------------------------------------------------------------------
# Reference and market data
# ---------------------------------------------------------------------------


class Instrument(DomainModel):
    """A tradeable instrument. ``key`` is ``"{exchange}:{series}:{symbol}"``, e.g. ``NSE:EQ:INFY``."""

    key: NonEmptyStr
    exchange: NonEmptyStr
    segment: NonEmptyStr
    symbol: NonEmptyStr
    series: NonEmptyStr
    isin: str = ""
    name: str = ""
    sector: str | None = None
    tick_size: Price = Decimal("0.05")
    lot_size: PosInt = 1
    broker_tokens: dict[str, str] = Field(default_factory=dict)
    mis_allowed: bool = True
    band_pct: PosFloat | None = None

    @classmethod
    def nse_equity(cls, symbol: str, **fields: object) -> Instrument:
        """An NSE cash-market equity (series EQ unless overridden)."""
        series = str(fields.pop("series", "EQ"))
        return cls.model_validate(
            {
                "key": f"NSE:{series}:{symbol}",
                "exchange": "NSE",
                "segment": "CM",
                "symbol": symbol,
                "series": series,
                **fields,
            }
        )

    @model_validator(mode="after")
    def _key_matches_parts(self) -> Self:
        expected = f"{self.exchange}:{self.series}:{self.symbol}"
        if self.key != expected:
            raise ValueError(f"instrument key {self.key!r} must be {expected!r}")
        return self

    def __hash__(self) -> int:
        return hash(self.key)


class Quote(DomainModel):
    """A point-in-time quote (audit §K.4). ``exchange_ts`` is when the price was true."""

    instrument_key: NonEmptyStr
    ltp: PosFloat
    bid: PosFloat | None = None
    ask: PosFloat | None = None
    prev_close: PosFloat | None = None
    volume_cum: NonNegInt = 0
    exchange_ts: AwareDatetime
    receipt_ts: AwareDatetime
    source: MarketDataSource
    is_delayed: bool = False
    quality_flags: tuple[str, ...] = ()

    def age_seconds(self, now: AwareDatetime) -> float:
        """Seconds between the exchange timestamp and ``now`` (not the receipt time)."""
        return (now - self.exchange_ts).total_seconds()


class Bar(DomainModel):
    """An OHLCV bar keyed by session date (audit §K.4)."""

    instrument_key: NonEmptyStr
    timeframe: Timeframe
    session_date: date
    open: PosFloat
    high: PosFloat
    low: PosFloat
    close: PosFloat
    volume: NonNegInt = 0
    is_settled: bool
    adjusted: bool = False
    source: MarketDataSource

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Self:
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close
        ):
            raise ValueError(
                f"inconsistent OHLC o={self.open} h={self.high} l={self.low} c={self.close}"
            )
        return self


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class SignalReason(DomainModel):
    """One structured fact behind a signal, e.g. ``name="rsi_14", value=31.2``."""

    name: NonEmptyStr
    value: float | str | bool | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _finite(self) -> Self:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError(f"signal reason {self.name!r} has a non-finite value")
        return self


class Signal(DomainModel):
    signal_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument_key: NonEmptyStr
    strategy: NonEmptyStr
    side: Side
    bar_date: date
    agreement_score: Probability
    stop_atr_mult: PosFloat
    target_atr_mult: PosFloat
    is_shadow: bool = False
    reasons: tuple[SignalReason, ...] = ()
    generated_at: AwareDatetime


class OrderIntent(DomainModel):
    """A proposal to trade (audit §H.2). ``quantity`` is None until risk sizes it."""

    intent_id: NonEmptyStr
    decision_id: NonEmptyStr
    parent_decision_id: str | None = None
    book_id: NonEmptyStr
    strategy: NonEmptyStr
    signal_id: str | None = None
    instrument: Instrument
    side: Side
    kind: IntentKind
    quantity: PosInt | None = None
    order_type: OrderType = OrderType.MARKET
    product: Product = Product.CNC
    validity: Validity = Validity.DAY
    limit_price: Price | None = None
    trigger_price: Price | None = None
    stop_price: Price | None = None
    target_price: Price | None = None
    reduce_only: bool
    decision_price: Price
    decision_ts: AwareDatetime
    reason: IntentReason
    source: IntentSource

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.reduce_only != self.kind.reduces_risk:
            raise ValueError(f"{self.kind} intents must have reduce_only={self.kind.reduces_risk}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT orders need limit_price")
        if self.order_type in (OrderType.SL, OrderType.SL_M) and self.trigger_price is None:
            raise ValueError(f"{self.order_type} orders need trigger_price")
        if self.order_type is OrderType.SL and self.limit_price is None:
            raise ValueError("SL orders need limit_price")
        return self


class Order(DomainModel):
    """An OMS order (audit §H.2, state machine §H.5). ``client_order_id`` = sha256(intent_id)[:16]."""

    client_order_id: NonEmptyStr
    intent: OrderIntent
    quantity: PosInt
    status: OrderStatus = OrderStatus.PENDING_NEW
    broker_order_id: str | None = None
    filled_qty: NonNegInt = 0
    avg_fill_price: Price | None = None
    arrival_price: Price | None = None
    submitted_at: AwareDatetime | None = None
    version: NonNegInt = 0

    @model_validator(mode="after")
    def _fill_bounds(self) -> Self:
        if self.filled_qty > self.quantity:
            raise ValueError(f"filled_qty {self.filled_qty} exceeds quantity {self.quantity}")
        if self.filled_qty > 0 and self.avg_fill_price is None:
            raise ValueError("a (partially) filled order needs avg_fill_price")
        return self

    @property
    def remaining_qty(self) -> int:
        return self.quantity - self.filled_qty


class Fill(DomainModel):
    fill_id: NonEmptyStr
    client_order_id: NonEmptyStr
    broker_order_id: str | None = None
    book_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument_key: NonEmptyStr
    side: Side
    quantity: PosInt
    price: Price
    ts: AwareDatetime
    charges: NonNegMoney = Decimal(0)
    charges_breakdown: dict[str, NonNegMoney] = Field(default_factory=dict)

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


class Position(DomainModel):
    """Net position per (book, instrument, product). ``quantity`` is signed (+long / -short)."""

    book_id: NonEmptyStr
    instrument_key: NonEmptyStr
    product: Product
    quantity: int
    avg_price: Price | None = None
    realized_pnl: Money = Decimal(0)  # net of charges
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _avg_price_iff_open(self) -> Self:
        if (self.quantity == 0) != (self.avg_price is None):
            raise ValueError("avg_price must be set exactly when the position is open")
        return self

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class RiskCheckResult(DomainModel):
    code: ReasonCode
    level: CheckLevel
    outcome: CheckOutcome
    observed: float | str | None = None
    limit: float | str | None = None
    message: str = ""
    max_qty: NonNegInt | None = None  # for RESIZE

    @model_validator(mode="after")
    def _level_matches_code(self) -> Self:
        if self.level is not self.code.level:
            raise ValueError(f"{self.code} is a {self.code.level}-level code, not {self.level}")
        if (self.outcome is CheckOutcome.RESIZE) != (self.max_qty is not None):
            raise ValueError("max_qty must be set exactly for RESIZE outcomes")
        return self


class RiskSnapshotSummary(DomainModel):
    """The portfolio state a risk decision was made against (audit §L.3 ``snapshot``)."""

    equity: Money
    sod_equity: Money
    day_pnl_mtm: Money
    peak_equity: Money
    gross_exposure: NonNegMoney
    net_exposure: Money
    heat: NonNegMoney
    open_positions: NonNegInt
    open_orders: NonNegInt
    quote_age_s: NonNegFloat | None = None
    data_source: MarketDataSource | None = None


class KillState(DomainModel):
    global_state: KillSwitchState = KillSwitchState.ARMED
    broker: KillSwitchState = KillSwitchState.ARMED
    strategies: dict[str, KillSwitchState] = Field(default_factory=dict)


class RiskDecision(EventPayload):
    """The binding pre-trade decision for one intent, persisted before routing (audit §L.3)."""

    event_type = "RiskDecision"

    decision_id: NonEmptyStr
    intent_id: NonEmptyStr
    client_order_id: str | None = None
    book_id: NonEmptyStr
    strategy: NonEmptyStr
    signal_id: str | None = None
    instrument_key: NonEmptyStr
    side: Side
    product: Product
    kind: IntentKind
    source: IntentSource
    qty_requested: PosInt | None = None
    qty_approved: NonNegInt
    ref_price: Price | None = None
    stop_price: Price | None = None
    target_price: Price | None = None
    advisor_verdict: Verdict | None = None
    advisor_kind: AdvisorKind | None = None
    outcome: RiskOutcome
    notional: NonNegMoney
    risk_amount: NonNegMoney
    risk_pct_equity: NonNegFloat
    reasons: tuple[RiskCheckResult, ...] = ()
    checks_run: tuple[str, ...] = ()
    limits_hash: NonEmptyStr
    snapshot: RiskSnapshotSummary
    kill_state: KillState = Field(default_factory=KillState)
    engine_version: NonEmptyStr
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def _outcome_consistent(self) -> Self:
        approved = self.outcome in (RiskOutcome.APPROVED, RiskOutcome.RESIZED)
        if approved != (self.qty_approved > 0):
            raise ValueError(
                f"{self.outcome} is inconsistent with qty_approved={self.qty_approved}"
            )
        if not approved and not self.reasons:
            raise ValueError(f"a {self.outcome} decision must carry its reasons")
        return self


# ---------------------------------------------------------------------------
# Advisors and typed events
# ---------------------------------------------------------------------------


class VerdictReason(DomainModel):
    """A claim with ``evidence_ref`` naming the input key it relies on (plan M6 prompts)."""

    claim: NonEmptyStr
    evidence_ref: NonEmptyStr


class AdvisorVerdict(EventPayload):
    """A per-book advisor's veto/approve on one proposal (AI invariants: §2 of the plan)."""

    event_type = "AdvisorVerdict"

    decision_id: NonEmptyStr
    book_id: NonEmptyStr
    advisor: AdvisorKind
    verdict: Verdict
    confidence: Probability | None = None
    reasons: tuple[VerdictReason, ...] = ()
    provider: str | None = None
    model: str | None = None
    latency_ms: NonNegFloat | None = None
    abstain_reason: str | None = None

    @model_validator(mode="after")
    def _abstain_explained(self) -> Self:
        if (self.verdict is Verdict.ABSTAIN) != (self.abstain_reason is not None):
            raise ValueError("abstain_reason must be set exactly for ABSTAIN verdicts")
        return self


class TypedEvent(EventPayload):
    """A classified exchange announcement (plan M7.7), stored with point-in-time ``published_at``."""

    event_type = "TypedEvent"

    event_id: NonEmptyStr
    instrument_key: NonEmptyStr
    published_at: AwareDatetime
    title: NonEmptyStr
    url: str | None = None
    source: NonEmptyStr
    relevant: bool
    announcement_type: AnnouncementType | None = None
    direction: EventDirection | None = None
    materiality: Materiality | None = None
    probabilities: dict[str, dict[str, Probability]] = Field(default_factory=dict)
    model: NonEmptyStr
    calibrated: bool
    classified_at: AwareDatetime
    extra: dict[str, JsonValue] = Field(default_factory=dict)
