"""
Execution Adapter Module

Order execution via DhanHQ sandbox or Local Paper Engine.
Provides abstraction layer for order placement with retry logic.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.config import get_settings
from src.execution.paper_engine import LocalPaperEngine

# DhanHQ is optional - only import if configured
try:
    from dhanhq import dhanhq

    DHANHQ_AVAILABLE = True
except ImportError:
    DHANHQ_AVAILABLE = False
    dhanhq = None

logger = logging.getLogger(__name__)


class OrderType(Enum):
    """Order types."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"  # Stop Loss
    SL_M = "SL-M"  # Stop Loss Market


class OrderSide(Enum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    """Order execution status."""

    PENDING = "pending"
    PLACED = "placed"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ProductType(Enum):
    """Product types for NSE."""

    CNC = "CNC"  # Cash & Carry (delivery)
    INTRADAY = "INTRADAY"  # Intraday
    MARGIN = "MARGIN"  # Margin trading


@dataclass
class OrderRequest:
    """Order request details."""

    symbol: str
    exchange: str  # NSE, BSE, NFO
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0  # For limit orders
    trigger_price: float = 0.0  # For SL orders
    product_type: ProductType = ProductType.INTRADAY

    # Metadata
    signal_id: str = ""
    strategy: str = ""


@dataclass
class OrderResult:
    """Order execution result."""

    order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_quantity: int = 0
    average_price: float = 0.0
    message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    broker_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.request.symbol,
            "side": self.request.side.value,
            "quantity": self.request.quantity,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
            "status": self.status.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "signal_id": self.request.signal_id,
            "strategy": self.request.strategy,
        }


@dataclass
class ExecutionAdapter:
    """
    Paper trading execution adapter.

    Handles order placement via DhanHQ sandbox with retry logic
    and proper error handling.
    """

    # Retry configuration
    max_retries: int = 3
    retry_delay: float = 1.0

    # Internal state
    _client: dhanhq = field(default=None, repr=False)
    _order_counter: int = field(default=0, repr=False)

    def __post_init__(self):
        """Initialize the DhanHQ client."""
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize DhanHQ client with credentials."""
        settings = get_settings()

        if settings.trading_mode == "paper":
            logger.info("Initializing paper trading adapter (sandbox mode)")
        else:
            logger.warning("LIVE TRADING MODE - Use with caution!")

        self._client = dhanhq(
            settings.dhan_client_id,
            settings.dhan_access_token.get_secret_value(),
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """
        Place an order via DhanHQ.

        Args:
            request: Order request details

        Returns:
            OrderResult with execution details
        """
        self._order_counter += 1
        order_id = f"ORD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{self._order_counter:04d}"

        logger.info(
            f"Placing order: {request.side.value} {request.quantity} {request.symbol} "
            f"@ {request.order_type.value}"
        )

        for attempt in range(self.max_retries):
            try:
                # Map order type
                dhan_order_type = self._map_order_type(request.order_type)

                # Place order via DhanHQ
                response = self._client.place_order(
                    security_id=request.symbol,
                    exchange_segment=self._map_exchange(request.exchange),
                    transaction_type=request.side.value,
                    quantity=request.quantity,
                    order_type=dhan_order_type,
                    product_type=request.product_type.value,
                    price=request.price if request.order_type == OrderType.LIMIT else 0,
                    trigger_price=request.trigger_price
                    if request.order_type in [OrderType.SL, OrderType.SL_M]
                    else 0,
                )

                # Parse response
                if response.get("status") == "success":
                    return OrderResult(
                        order_id=response.get("data", {}).get("orderId", order_id),
                        request=request,
                        status=OrderStatus.PLACED,
                        message="Order placed successfully",
                        broker_response=response,
                    )
                else:
                    error_msg = response.get("remarks", "Unknown error")
                    logger.warning(f"Order rejected: {error_msg}")
                    return OrderResult(
                        order_id=order_id,
                        request=request,
                        status=OrderStatus.REJECTED,
                        message=error_msg,
                        broker_response=response,
                    )

            except Exception as e:
                logger.error(f"Order placement failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))

        # All retries failed
        return OrderResult(
            order_id=order_id,
            request=request,
            status=OrderStatus.FAILED,
            message=f"Failed after {self.max_retries} attempts",
        )

    async def get_order_status(self, order_id: str) -> OrderResult | None:
        """Get the current status of an order."""
        try:
            response = self._client.get_order_by_id(order_id)

            if response.get("status") == "success":
                data = response.get("data", {})
                return OrderResult(
                    order_id=order_id,
                    request=OrderRequest(
                        symbol=data.get("securityId", ""),
                        exchange=data.get("exchangeSegment", "NSE"),
                        side=OrderSide(data.get("transactionType", "BUY")),
                        quantity=data.get("quantity", 0),
                    ),
                    status=self._map_status(data.get("orderStatus", "")),
                    filled_quantity=data.get("filledQty", 0),
                    average_price=data.get("avgPrice", 0),
                    broker_response=response,
                )
        except Exception as e:
            logger.error(f"Failed to get order status: {e}")

        return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            response = self._client.cancel_order(order_id)
            return response.get("status") == "success"
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            return False

    async def get_positions(self) -> list[dict[str, Any]]:
        """Get current open positions."""
        try:
            response = self._client.get_positions()
            if response.get("status") == "success":
                return response.get("data", [])
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
        return []

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Get current holdings."""
        try:
            response = self._client.get_holdings()
            if response.get("status") == "success":
                return response.get("data", [])
        except Exception as e:
            logger.error(f"Failed to get holdings: {e}")
        return []

    def _map_order_type(self, order_type: OrderType) -> str:
        """Map internal order type to DhanHQ format."""
        mapping = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT: "LIMIT",
            OrderType.SL: "STOP_LOSS",
            OrderType.SL_M: "STOP_LOSS_MARKET",
        }
        return mapping.get(order_type, "MARKET")

    def _map_exchange(self, exchange: str) -> str:
        """Map exchange to DhanHQ segment."""
        mapping = {
            "NSE": "NSE_EQ",
            "BSE": "BSE_EQ",
            "NFO": "NSE_FNO",
            "MCX": "MCX_COMM",
        }
        return mapping.get(exchange.upper(), "NSE_EQ")

    def _map_status(self, status: str) -> OrderStatus:
        """Map DhanHQ status to internal status."""
        mapping = {
            "PENDING": OrderStatus.PENDING,
            "TRANSIT": OrderStatus.PENDING,
            "TRADED": OrderStatus.FILLED,
            "PARTIALLY_TRADED": OrderStatus.PARTIALLY_FILLED,
            "REJECTED": OrderStatus.REJECTED,
            "CANCELLED": OrderStatus.CANCELLED,
        }
        return mapping.get(status.upper(), OrderStatus.PENDING)


async def execute_trades(
    trades: list[dict[str, Any]],
    adapter: "ExecutionAdapter | LocalExecutionAdapter | None" = None,
    market_prices: dict[str, float] | None = None,
) -> list[OrderResult]:
    """
    Execute a list of approved trades.

    Args:
        trades: List of trade dictionaries from risk agent
        adapter: Execution adapter (creates new based on config if None)
        market_prices: Current market prices (required for local paper trading)

    Returns:
        List of order results
    """
    settings = get_settings()

    # Select adapter based on execution mode
    if adapter is None:
        if settings.execution_mode == "local_paper":
            adapter = LocalExecutionAdapter()
        elif settings.execution_mode in ["dhan_paper", "live"]:
            if DHANHQ_AVAILABLE and settings.dhan_client_id:
                adapter = ExecutionAdapter()
            else:
                logger.warning("DhanHQ not available, falling back to local paper")
                adapter = LocalExecutionAdapter()
        else:
            adapter = LocalExecutionAdapter()

    results = []

    for trade in trades:
        entry_price = trade.get("entry_price", 0)

        # For local paper trading, use market prices
        if isinstance(adapter, LocalExecutionAdapter) and market_prices:
            symbol = trade.get("symbol", "")
            if symbol in market_prices:
                entry_price = market_prices[symbol]

        # Create order request from trade
        request = OrderRequest(
            symbol=trade.get("symbol", ""),
            exchange=trade.get("exchange", "NSE"),
            side=OrderSide.BUY if trade.get("signal_type") == "BUY" else OrderSide.SELL,
            quantity=_calculate_quantity(trade),
            order_type=OrderType.MARKET,
            price=entry_price,
            signal_id=trade.get("signal_id", ""),
            strategy=trade.get("strategy", ""),
        )

        result = await adapter.place_order(request)
        results.append(result)

    return results


def _calculate_quantity(trade: dict[str, Any]) -> int:
    """Calculate order quantity based on position size and price."""
    settings = get_settings()

    position_pct = trade.get("position_size_pct", 5.0)
    entry_price = trade.get("entry_price", 0)

    if entry_price <= 0:
        return 1

    # Calculate based on max position size
    capital = settings.max_position_size
    position_value = capital * (position_pct / 100)
    quantity = int(position_value / entry_price)

    return max(1, quantity)  # At least 1 share


# ===========================================
# Local Paper Execution Adapter
# ===========================================


@dataclass
class LocalExecutionAdapter:
    """
    Local paper trading execution adapter.

    Uses LocalPaperEngine for 100% free paper trading.
    No broker connection required.
    """

    _engine: LocalPaperEngine = field(default=None, repr=False)

    def __post_init__(self):
        """Initialize the local paper engine."""
        if self._engine is None:
            self._engine = LocalPaperEngine()
        logger.info("Initialized Local Paper Execution Adapter")

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """
        Place an order via local paper engine.

        Args:
            request: Order request details

        Returns:
            OrderResult with execution details
        """
        logger.info(
            f"[LOCAL] Placing order: {request.side.value} {request.quantity} {request.symbol} "
            f"@ {request.order_type.value}"
        )

        # Execute via paper engine
        order = self._engine.place_order(
            symbol=request.symbol,
            side=request.side.value,
            quantity=request.quantity,
            current_price=request.price,
            order_type=request.order_type.value,
        )

        # Map status
        status_map = {
            "FILLED": OrderStatus.FILLED,
            "REJECTED": OrderStatus.REJECTED,
            "PENDING": OrderStatus.PENDING,
        }

        return OrderResult(
            order_id=order.order_id,
            request=request,
            status=status_map.get(order.status, OrderStatus.PENDING),
            filled_quantity=request.quantity if order.status == "FILLED" else 0,
            average_price=order.price,
            message=f"Local paper: {order.status}",
            broker_response={"engine": "local_paper"},
        )

    async def get_positions(self) -> list[dict[str, Any]]:
        """Get current open positions from paper engine."""
        return [p.to_dict() for p in self._engine.get_positions()]

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Get current holdings (same as positions for paper)."""
        return await self.get_positions()

    def get_stats(self) -> dict[str, Any]:
        """Get trading statistics."""
        return self._engine.get_stats()

    def get_balance(self) -> float:
        """Get current cash balance."""
        return self._engine.get_balance()
