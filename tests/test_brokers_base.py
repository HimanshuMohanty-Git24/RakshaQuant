"""Plan M3.1: the broker contract's error taxonomy and capability records."""

from decimal import Decimal

import pytest

from src.brokers.base import (
    AuthError,
    BrokerCapabilities,
    BrokerError,
    BrokerUnavailableError,
    InvalidRequestError,
    MarginQuote,
    OrderRejectedError,
    RateLimitedError,
    RejectReason,
    TransportError,
)
from src.domain.types import OrderType, Product, Validity


@pytest.mark.parametrize(
    ("error", "outcome", "retryable"),
    [
        (AuthError("expired"), "NOT_PLACED", False),
        (RateLimitedError("slow down", retry_after=2.0), "NOT_PLACED", True),
        (
            OrderRejectedError("no cash", reason=RejectReason.INSUFFICIENT_FUNDS),
            "NOT_PLACED",
            False,
        ),
        (InvalidRequestError("qty 0"), "NOT_PLACED", False),
        (TransportError("timeout"), "UNKNOWN", True),
        (BrokerUnavailableError("503"), "UNKNOWN", True),
    ],
)
def test_error_outcomes_drive_the_oms(error, outcome, retryable):
    assert isinstance(error, BrokerError)
    assert error.outcome == outcome and error.retryable is retryable


def test_error_details_are_kept():
    err = OrderRejectedError(
        "band", reason=RejectReason.PRICE_BAND, broker_code="RMS:7", raw={"x": 1}
    )
    assert err.reason is RejectReason.PRICE_BAND and err.broker_code == "RMS:7"
    assert dict(err.raw) == {"x": 1}
    assert RateLimitedError("x", retry_after=1.5).retry_after == 1.5


def test_capabilities_supports():
    caps = BrokerCapabilities(
        order_types=frozenset({OrderType.MARKET, OrderType.SL_M}),
        products=frozenset({Product.CNC}),
    )
    assert caps.supports(OrderType.MARKET, Product.CNC, Validity.DAY)
    assert not caps.supports(OrderType.LIMIT, Product.CNC, Validity.DAY)
    assert not caps.supports(OrderType.MARKET, Product.MIS, Validity.DAY)
    assert not caps.supports(OrderType.MARKET, Product.CNC, Validity.IOC)


def test_margin_quote():
    assert MarginQuote(required=Decimal(100), available=Decimal(100)).sufficient
    assert not MarginQuote(required=Decimal("100.01"), available=Decimal(100)).sufficient
