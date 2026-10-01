"""
The broker adapter contract (plan M3.1; audit §H.2-§H.4).

The OMS talks to every broker - simulated, backtest, Dhan, ... - through :class:`BrokerAdapter`.
Adapters own transport, auth, rate limits and mapping; **never** business logic.

Rules every adapter follows:

* ``place_order`` sends the OMS ``client_order_id`` as the broker tag and **never retries** a
  write. If the outcome is unknown (timeout, reset, 5xx) it raises an error whose ``outcome`` is
  ``"UNKNOWN"`` and the OMS resolves the order with ``find_order_by_tag``.
* Errors are normalised to the taxonomy below; ``outcome`` drives the OMS state machine.
* Reads may be retried; writes may not.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime

from src.domain.base import DomainModel, NonEmptyStr, NonNegInt, NonNegMoney, Price
from src.domain.types import (
    Fill,
    Instrument,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    Product,
    Quote,
    Side,
    Validity,
)

Outcome = Literal["NOT_PLACED", "UNKNOWN", "N/A"]


# ---------------------------------------------------------------------------
# Capabilities (audit §H.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateSpec:
    per_second: float
    burst: int = 1


@dataclass(frozen=True)
class BrokerCapabilities:
    order_types: frozenset[OrderType]
    products: frozenset[Product]
    validities: frozenset[Validity] = frozenset({Validity.DAY})
    supports_modify: bool = False
    supports_bracket: bool = False
    supports_gtt: bool = False
    tag_max_len: int = 20
    lookup_by_tag: bool = False
    order_stream: bool = False
    rate_limits: Mapping[str, RateSpec] = field(default_factory=dict)  # "orders", "data", ...
    session_ttl_s: float | None = None
    mis_square_off: time | None = None

    def supports(self, order_type: OrderType, product: Product, validity: Validity) -> bool:
        return (
            order_type in self.order_types
            and product in self.products
            and validity in self.validities
        )


# ---------------------------------------------------------------------------
# Broker-side records
# ---------------------------------------------------------------------------


class BrokerAck(DomainModel):
    client_order_id: NonEmptyStr
    broker_order_id: NonEmptyStr
    status: OrderStatus
    ts: AwareDatetime
    message: str = ""


class BrokerOrderSnapshot(DomainModel):
    """The broker's view of one order."""

    broker_order_id: NonEmptyStr
    client_order_id: NonEmptyStr
    instrument_key: NonEmptyStr
    side: Side
    product: Product
    order_type: OrderType
    quantity: NonNegInt
    filled_qty: NonNegInt
    avg_fill_price: Price | None = None
    trigger_price: Price | None = None
    status: OrderStatus
    updated_at: AwareDatetime
    message: str = ""


class Holding(DomainModel):
    """Settled delivery holdings (CNC)."""

    instrument_key: NonEmptyStr
    quantity: NonNegInt
    avg_price: Price


class Funds(DomainModel):
    available_cash: NonNegMoney
    blocked: NonNegMoney = Decimal(0)  # reserved for open buy orders
    unsettled_credit: NonNegMoney = Decimal(0)  # sale proceeds not yet available


class MarginQuote(DomainModel):
    required: NonNegMoney
    available: NonNegMoney

    @property
    def sufficient(self) -> bool:
        return self.required <= self.available


class Subscription(Protocol):
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Error taxonomy (audit §H.4)
# ---------------------------------------------------------------------------


class RejectReason(StrEnum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    PRICE_BAND = "PRICE_BAND"
    TICK_SIZE = "TICK_SIZE"
    QTY_VALUE_LIMIT = "QTY_VALUE_LIMIT"
    MARKET_CLOSED = "MARKET_CLOSED"
    PRODUCT_NOT_ALLOWED = "PRODUCT_NOT_ALLOWED"
    SHORT_NOT_ALLOWED = "SHORT_NOT_ALLOWED"
    INSTRUMENT_RESTRICTED = "INSTRUMENT_RESTRICTED"
    RMS_OTHER = "RMS_OTHER"


class BrokerError(Exception):
    """Base of every normalised broker error. ``outcome`` tells the OMS what happened to a write."""

    retryable: bool = False
    outcome: Outcome = "N/A"

    def __init__(
        self, message: str, *, broker_code: str = "", raw: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.broker_code = broker_code
        self.raw: Mapping[str, Any] = raw or {}


class AuthError(BrokerError):
    """Token expired or invalid: the order was not placed; renew with ``ensure_session``."""

    outcome = "NOT_PLACED"


class RateLimitedError(BrokerError):
    outcome = "NOT_PLACED"
    retryable = True

    def __init__(self, message: str, *, retry_after: float, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class TransportError(BrokerError):
    """Timeout or connection reset. For a write the order may or may not exist."""

    outcome = "UNKNOWN"
    retryable = True


class BrokerUnavailableError(BrokerError):
    """5xx or an open circuit breaker. For a write the order may or may not exist."""

    outcome = "UNKNOWN"
    retryable = True


class OrderRejectedError(BrokerError):
    outcome = "NOT_PLACED"

    def __init__(self, message: str, *, reason: RejectReason, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.reason = reason


class InvalidRequestError(BrokerError):
    """Failed local validation before anything was sent."""

    outcome = "NOT_PLACED"


# ---------------------------------------------------------------------------
# The adapter protocol (audit §H.2)
# ---------------------------------------------------------------------------

QuoteCallback = Callable[[Quote], None]
OrderUpdateCallback = Callable[[BrokerOrderSnapshot], None]
FillCallback = Callable[[Fill], None]


class BrokerAdapter(Protocol):
    name: str

    def capabilities(self) -> BrokerCapabilities: ...

    async def authenticate(self) -> None: ...

    async def ensure_session(self) -> None:
        """Renew the session before it expires."""
        ...

    async def load_instruments(self) -> list[Instrument]: ...

    async def subscribe_market_data(
        self,
        instruments: Sequence[Instrument],
        on_quote: QuoteCallback,
        mode: Literal["ltp", "quote", "depth"],
    ) -> Subscription: ...

    async def get_quote(self, instruments: Sequence[Instrument]) -> dict[str, Quote]: ...

    async def place_order(self, order: Order) -> BrokerAck:
        """MUST send ``order.client_order_id``; MUST NOT retry."""
        ...

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        qty: int | None = None,
        price: Decimal | None = None,
        trigger: Decimal | None = None,
    ) -> BrokerAck: ...

    async def cancel_order(self, broker_order_id: str) -> BrokerAck: ...

    async def get_order(self, broker_order_id: str) -> BrokerOrderSnapshot: ...

    async def find_order_by_tag(self, client_order_id: str) -> BrokerOrderSnapshot | None: ...

    async def get_orders(self) -> list[BrokerOrderSnapshot]: ...

    async def get_trades(self, since: datetime | None = None) -> list[Fill]: ...

    async def subscribe_order_updates(
        self, on_update: OrderUpdateCallback, on_fill: FillCallback
    ) -> Subscription: ...

    async def get_positions(self) -> list[Position]: ...

    async def get_holdings(self) -> list[Holding]: ...

    async def get_funds(self) -> Funds: ...

    async def get_margin_required(self, intents: Sequence[OrderIntent]) -> MarginQuote: ...
