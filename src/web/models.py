"""
The web API's response models (plan M9.2). They are the contract: FastAPI turns them into the
OpenAPI document that ``frontend/src/api/types.gen.ts`` is generated from (M9.5).

Money and prices are exact decimals, serialised as strings; timestamps are timezone-aware.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(frozen=True)


# -- summary ---------------------------------------------------------------------------------


class SessionInfo(ApiModel):
    date: date
    state: str


class BookSummary(ApiModel):
    book_id: str
    advisor: str
    equity: Decimal | None
    cash: Decimal | None
    unrealized_pnl: Decimal | None
    day_pnl: Decimal | None
    valued_at: datetime | None
    valuation: Literal["live", "last_mark", "none"]
    realized_pnl_today: Decimal
    open_positions: int
    open_orders: int
    trades_today: int
    kill_switch: str  # the book's global switch: ARMED / HALT_NEW / FLATTEN


class Summary(ApiModel):
    environment: str
    mode: Literal["paper"] = "paper"  # the v2 engine has no broker path
    demo: bool
    running: bool
    experiment: str
    session: SessionInfo | None
    market_open: bool
    now: datetime
    last_seq: int
    books: list[BookSummary]


# -- blotter ---------------------------------------------------------------------------------


class PositionRow(ApiModel):
    book_id: str
    instrument_key: str
    symbol: str
    product: str
    quantity: int
    avg_price: Decimal | None
    realized_pnl: Decimal
    mark: Decimal | None
    unrealized_pnl: Decimal | None
    updated_ts: datetime


class OrderRow(ApiModel):
    client_order_id: str
    book_id: str
    decision_id: str
    intent_id: str
    instrument_key: str
    symbol: str
    strategy: str
    side: str
    product: str
    order_type: str
    kind: str
    reason: str
    quantity: int
    status: str
    filled_qty: int
    avg_fill_price: Decimal | None
    broker_order_id: str | None
    last_message: str | None
    ist_date: date
    updated_ts: datetime


class FillRow(ApiModel):
    fill_id: str
    client_order_id: str
    book_id: str
    decision_id: str
    instrument_key: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    charges: Decimal
    ts: datetime


class TradeRow(ApiModel):
    trade_id: str
    book_id: str
    decision_id: str
    exit_decision_id: str | None
    instrument_key: str
    symbol: str
    strategy: str
    side: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    entry_ts: datetime
    exit_ts: datetime
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    exit_reason: str


# -- decisions ---------------------------------------------------------------------------------


class DecisionRow(ApiModel):
    """What happened to one signal in one book (a ``SignalDisposition``)."""

    seq: int
    ts: datetime
    decision_id: str
    signal_id: str
    book_id: str
    instrument_key: str
    symbol: str
    strategy: str
    disposition: str
    detail: str
    client_order_id: str | None


class LineageEvent(ApiModel):
    seq: int
    ts: datetime
    type: str
    decision_id: str | None
    book_id: str | None
    source: str
    data: dict[str, Any]


class Lineage(ApiModel):
    """Everything recorded under a decision, plus its exits' decisions, in ``seq`` order."""

    decision_id: str
    exit_decision_ids: list[str]
    events: list[LineageEvent]


# -- risk ------------------------------------------------------------------------------------


class KillSwitchRow(ApiModel):
    scope: str
    name: str
    state: str
    reason: str
    actor: str
    since: datetime


class BookRisk(ApiModel):
    book_id: str
    kill_switches: list[KillSwitchRow]
    daily_state: dict[str, Any] | None
    rejections_today: dict[str, int]  # reason code -> blocked checks today


class RiskView(ApiModel):
    limits_hash: str
    limits: dict[str, Any]
    books: list[BookRisk]


# -- books (the paired experiment) -------------------------------------------------------------


class VetoPrecision(ApiModel):
    vetoes: int
    settled: int
    correct: int
    precision: float | None
    ci95: tuple[float, float] | None
    loss_avoided_inr: float
    gain_forgone_inr: float


class BookComparison(ApiModel):
    book_id: str
    advisor: str
    capital: Decimal
    equity: Decimal | None
    valuation: Literal["live", "last_mark", "none"]
    return_pct: float | None
    realized_net_to_date: Decimal
    closed_trades: int
    win_rate: float | None
    ai_spend_inr_to_date: Decimal
    vs: str | None  # the baseline book (None for the baseline itself)
    equity_difference_inr: Decimal | None
    net_ai_value_inr: Decimal | None
    veto_precision: VetoPrecision


class BooksView(ApiModel):
    experiment: str
    baseline: str
    books: list[BookComparison]


# -- AI ----------------------------------------------------------------------------------------


class LLMCallRow(ApiModel):
    seq: int
    ts: datetime
    decision_id: str | None
    book_id: str | None
    role: str
    provider: str
    model: str
    attempt: int
    prompt_version: str
    outcome: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    cost_usd: Decimal | None
    cost_inr: Decimal | None
    cache_hit: bool


class SpendRow(ApiModel):
    key: str
    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    cost_inr: Decimal


class SpendView(ApiModel):
    group_by: Literal["role", "book", "day", "model"]
    rows: list[SpendRow]
    total_inr: Decimal


class ModelHealth(ApiModel):
    model: str
    calls_today: int
    errors_today: int
    p95_latency_ms: float | None
    last_outcome: str | None
    last_call: datetime | None


class RoleModels(ApiModel):
    role: str
    enabled: bool
    chain: list[str]  # provider:model, first = primary
    models: list[ModelHealth]


class DecisionModelStats(ApiModel):
    task: str
    model: str
    checkpoint: str
    calls: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    escalation_rate: float | None
    calibrated_rate: float | None
    shadow_calls: int
    outcomes: dict[str, int]


# -- market, events, reports, system, config ------------------------------------------------------


class BarRow(ApiModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class Bars(ApiModel):
    symbol: str
    instrument_key: str
    adjusted: bool
    source: Literal["engine", "tape", "none"]
    bars: list[BarRow]


class TypedEventRow(ApiModel):
    event_id: str
    instrument_key: str
    symbol: str
    published_at: datetime
    relevant: bool
    announcement_type: str | None
    direction: str | None
    materiality: str | None
    data: dict[str, Any]


class ReconcileRow(ApiModel):
    book_id: str
    scope: str
    in_sync: bool
    diffs: list[str]
    ts: datetime


class SystemView(ApiModel):
    environment: str
    running: bool
    pid: int
    tasks: list[str]
    uptime_s: float | None
    loop_lag_ms: float | None
    last_heartbeat: datetime | None
    last_loop_lag: datetime | None
    quote_age_s: dict[str, float]  # data source -> age of its freshest quote
    store_bytes: int
    store_path: str
    last_seq: int
    schema_version: int
    last_reconcile: list[ReconcileRow]
    versions: dict[str, str]


class ConfigView(ApiModel):
    """Read-only and redacted: names and switches, never a key, URL credential or token."""

    environment: str
    mode: Literal["paper"] = "paper"
    execution_mode_requested: str
    execution_mode_note: str | None
    market_data_source: str
    experiment: dict[str, Any]
    llm_roles: dict[str, list[str]]
    decision_models: dict[str, Any]
    announcements_enabled: bool
    telegram_configured: bool
    limits_hash: str
    read_only: bool
