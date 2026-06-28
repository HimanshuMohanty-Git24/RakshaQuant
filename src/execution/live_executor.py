"""
Live broker order lifecycle + position reconciliation.

The Dhan `ExecutionAdapter` only *submits* an order and returns PLACED — it never confirms
what actually happened. `LiveBrokerExecutor` closes that gap: it submits, then polls order
status to a terminal state (filled / partially filled / rejected / cancelled), so the system
knows the real fill instead of assuming success. `reconcile_positions` compares local
positions against the broker's so drift is detected (broker is the source of truth).

This is wired behind the `allow_live_orders` master gate and is exercised in tests with a
mocked adapter — no real broker calls happen by default.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from src.execution.adapter import ExecutionAdapter, OrderRequest, OrderResult, OrderStatus

logger = logging.getLogger(__name__)

_TERMINAL = {
    OrderStatus.FILLED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELLED,
    OrderStatus.FAILED,
}


@dataclass
class LiveBrokerExecutor:
    """Submits an order and polls until its fill status is known (or times out)."""

    adapter: ExecutionAdapter
    poll_attempts: int = 5
    poll_delay: float = 1.0

    async def place_and_confirm(
        self, request: OrderRequest, client_order_id: str = ""
    ) -> OrderResult:
        """
        Place an order and poll its status to a terminal state.

        Returns the confirmed OrderResult (FILLED/PARTIALLY_FILLED/REJECTED/...). If the broker
        never reports a terminal status within the poll budget, returns the original PLACED
        result tagged so the caller knows the fill is unconfirmed (it must NOT assume filled).
        """
        placed = await self.adapter.place_order(request)
        if placed.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
            return placed

        order_id = placed.order_id
        for attempt in range(self.poll_attempts):
            status = await self.adapter.get_order_status(order_id)
            if status is not None and status.status in _TERMINAL:
                logger.info(
                    "Live order %s confirmed: %s (filled %s @ %s)",
                    order_id, status.status.value, status.filled_quantity, status.average_price,
                )
                return status
            if attempt < self.poll_attempts - 1:
                await asyncio.sleep(self.poll_delay)

        logger.warning(
            "Live order %s not confirmed within %d polls — treating as UNCONFIRMED, not filled.",
            order_id, self.poll_attempts,
        )
        placed.message = "fill unconfirmed within poll budget"
        return placed


@dataclass
class ReconciliationReport:
    """Result of comparing local positions to the broker's."""

    in_sync: bool
    broker_only: list[str] = field(default_factory=list)       # at broker, not local
    local_only: list[str] = field(default_factory=list)        # local, not at broker
    quantity_mismatches: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        if self.in_sync:
            return "positions in sync with broker"
        parts = []
        if self.broker_only:
            parts.append(f"{len(self.broker_only)} broker-only ({', '.join(self.broker_only)})")
        if self.local_only:
            parts.append(f"{len(self.local_only)} local-only ({', '.join(self.local_only)})")
        if self.quantity_mismatches:
            parts.append(f"{len(self.quantity_mismatches)} qty mismatches")
        return "position drift: " + "; ".join(parts)


def _normalize(positions: list[Any]) -> dict[str, int]:
    """Map a list of positions (dicts or objects) to {symbol: net_quantity}."""
    out: dict[str, int] = {}
    for pos in positions:
        if isinstance(pos, dict):
            symbol = pos.get("symbol", "")
            qty = pos.get("quantity", pos.get("netQty", 0))
            side = pos.get("side", "BUY")
        else:
            symbol = getattr(pos, "symbol", "")
            qty = getattr(pos, "quantity", 0)
            side = getattr(pos, "side", "BUY")
        if not symbol:
            continue
        quantity = int(qty) if qty is not None else 0
        signed = quantity if str(side).upper() == "BUY" else -quantity
        out[symbol] = out.get(symbol, 0) + signed
    return out


def reconcile_positions(
    local_positions: list[Any], broker_positions: list[Any]
) -> ReconciliationReport:
    """
    Compare local vs broker positions by symbol and net quantity (broker = source of truth).

    Pure function (takes already-fetched lists) so it is trivially testable.
    """
    local = _normalize(local_positions)
    broker = _normalize(broker_positions)

    broker_only = sorted(s for s in broker if s not in local)
    local_only = sorted(s for s in local if s not in broker)
    mismatches = [
        {"symbol": s, "local_qty": local[s], "broker_qty": broker[s]}
        for s in sorted(set(local) & set(broker))
        if local[s] != broker[s]
    ]
    in_sync = not (broker_only or local_only or mismatches)
    return ReconciliationReport(
        in_sync=in_sync, broker_only=broker_only, local_only=local_only,
        quantity_mismatches=mismatches,
    )
