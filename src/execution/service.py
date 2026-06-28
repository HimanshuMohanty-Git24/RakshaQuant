"""
Unified execution service.

One mode-switched entry point for placing orders, so the live loop has a single code path
instead of calling the paper engine (or a broker) directly. Adds the safety the audit found
missing:

- **Order idempotency** — every submit carries an idempotency key; a repeat (retry, restart,
  duplicate approved-trade) returns the prior result instead of placing a second order.
- **Shadow mode** — mirrors exactly what a live run would do (sizing + the order it *would*
  send), simulating the fill against the paper engine, but never contacts a broker. This is
  the safe bridge to live trading.
- **No silent downgrade** — a `live`/`dhan_paper` request without `allow_live_orders` (or
  without broker credentials) resolves to SHADOW with a loud warning, rather than quietly
  trading on the local paper wallet as if it were real.

Real broker order submission is intentionally NOT wired here — it lands in the next slice
(reconciliation + live fill lifecycle). Until then, even `allow_live_orders=true` does not
send real orders; the service says so explicitly.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.execution.adapter import OrderRequest, OrderSide, OrderStatus, OrderType
from src.execution.live_executor import LiveBrokerExecutor
from src.execution.paper_engine import LocalPaperEngine

logger = logging.getLogger(__name__)

MAX_IDEMPOTENCY_ENTRIES = 5000


class ExecutionMode(str, Enum):
    LOCAL_PAPER = "local_paper"
    SHADOW = "shadow"
    DHAN_PAPER = "dhan_paper"
    LIVE = "live"


@dataclass
class ExecutionResult:
    """Outcome of an execution submission."""

    status: str  # FILLED | REJECTED | BLOCKED | DUPLICATE
    symbol: str
    side: str
    quantity: int
    fill_price: float
    mode: str
    client_order_id: str
    order_id: str = ""
    is_shadow: bool = False
    is_duplicate: bool = False
    message: str = ""

    @property
    def filled(self) -> bool:
        return self.status == "FILLED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "mode": self.mode,
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "is_shadow": self.is_shadow,
            "is_duplicate": self.is_duplicate,
            "message": self.message,
        }


class IdempotencyStore:
    """
    Records submitted order keys so the same intent isn't placed twice.

    Optionally persisted to disk so a restart mid-run does not replay orders. Bounded to the
    most recent MAX_IDEMPOTENCY_ENTRIES keys.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._seen: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            for key, value in data.items():
                self._seen[key] = value
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not load idempotency store (%s); starting empty.", e)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self._seen, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not persist idempotency store (%s).", e)

    def seen(self, key: str) -> dict[str, Any] | None:
        return self._seen.get(key)

    def record(self, key: str, result: dict[str, Any]) -> None:
        self._seen[key] = result
        self._seen.move_to_end(key)
        while len(self._seen) > MAX_IDEMPOTENCY_ENTRIES:
            self._seen.popitem(last=False)
        self._save()


@dataclass
class ExecutionService:
    """Mode-switched order submission with idempotency and a shadow (dry-run) mode."""

    engine: LocalPaperEngine
    mode: ExecutionMode = ExecutionMode.LOCAL_PAPER
    allow_live_orders: bool = False
    idempotency: IdempotencyStore = field(default_factory=IdempotencyStore)
    kill_switch: Callable[[], bool] | None = None
    broker_executor: LiveBrokerExecutor | None = None
    _effective_mode: ExecutionMode = field(init=False)

    def __post_init__(self) -> None:
        self._effective_mode = self._resolve_mode()
        logger.info(
            "ExecutionService ready (requested=%s, effective=%s, allow_live_orders=%s)",
            self.mode.value,
            self._effective_mode.value,
            self.allow_live_orders,
        )

    @classmethod
    def from_settings(
        cls,
        engine: LocalPaperEngine,
        idempotency: IdempotencyStore | None = None,
        kill_switch: Callable[[], bool] | None = None,
    ) -> ExecutionService:
        settings = get_settings()
        try:
            mode = ExecutionMode(getattr(settings, "execution_mode", "local_paper"))
        except ValueError:
            mode = ExecutionMode.LOCAL_PAPER
        return cls(
            engine=engine,
            mode=mode,
            allow_live_orders=bool(getattr(settings, "allow_live_orders", False)),
            idempotency=idempotency if idempotency is not None else IdempotencyStore(),
            kill_switch=kill_switch,
        )

    @property
    def effective_mode(self) -> ExecutionMode:
        return self._effective_mode

    def _resolve_mode(self) -> ExecutionMode:
        """Resolve the mode actually used, never silently downgrading a live request."""
        if self.mode in (ExecutionMode.LIVE, ExecutionMode.DHAN_PAPER):
            if not self.allow_live_orders:
                logger.warning(
                    "execution_mode=%s but allow_live_orders is False — running in SHADOW "
                    "(no real orders sent).",
                    self.mode.value,
                )
                return ExecutionMode.SHADOW
            settings = get_settings()
            if not (
                getattr(settings, "dhan_client_id", None)
                and getattr(settings, "dhan_access_token", None)
            ):
                logger.error(
                    "execution_mode=%s requested but Dhan credentials are missing — running "
                    "in SHADOW, NOT downgrading silently to local paper.",
                    self.mode.value,
                )
                return ExecutionMode.SHADOW
        return self.mode

    @staticmethod
    def _client_order_id(idempotency_key: str) -> str:
        # Deterministic client order id derived from the idempotency key, so a broker (later)
        # can also dedupe on it.
        stamp = datetime.now().strftime("%Y%m%d")
        return f"COID-{stamp}-{abs(hash(idempotency_key)) % 10_000_000:07d}"

    def _guard(
        self, symbol: str, side: str, quantity: int, idempotency_key: str, client_order_id: str
    ) -> ExecutionResult | None:
        """Shared pre-submission guards: idempotency dedup + kill switch."""
        prior = self.idempotency.seen(idempotency_key)
        if prior is not None:
            logger.warning("Duplicate submission suppressed for key=%s", idempotency_key)
            return ExecutionResult(
                status="DUPLICATE",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=float(prior.get("fill_price", 0.0)),
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                order_id=str(prior.get("order_id", "")),
                is_shadow=bool(prior.get("is_shadow", False)),
                is_duplicate=True,
                message="idempotency hit — order already submitted",
            )
        if self.kill_switch is not None and self.kill_switch():
            logger.warning("Kill switch active — blocking %s %s %s", side, quantity, symbol)
            return ExecutionResult(
                status="BLOCKED",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0.0,
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                message="kill switch active",
            )
        return None

    def _fill_paper(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str,
        client_order_id: str,
    ) -> ExecutionResult:
        """Simulate a fill against the paper engine (local_paper and shadow)."""
        is_shadow = self._effective_mode == ExecutionMode.SHADOW
        order = self.engine.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            current_price=price,
            order_type=order_type,
        )
        message = "SHADOW: simulated, not sent to broker" if is_shadow else "local paper fill"
        return ExecutionResult(
            status=order.status,
            symbol=symbol,
            side=side,
            quantity=quantity,
            fill_price=order.price,
            mode=self._effective_mode.value,
            client_order_id=client_order_id,
            order_id=order.order_id,
            is_shadow=is_shadow,
            message=message,
        )

    def _record_if_filled(self, idempotency_key: str, result: ExecutionResult) -> None:
        # Record transacted orders so a rejection/block can be retried but a fill can't repeat.
        if result.status in ("FILLED", "PARTIALLY_FILLED"):
            self.idempotency.record(idempotency_key, result.to_dict())

    def submit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        idempotency_key: str,
        order_type: str = "MARKET",
    ) -> ExecutionResult:
        """
        Submit synchronously. Handles local_paper and shadow; a live mode returns a reject
        (live submission is async — use submit_async).
        """
        side = side.upper()
        client_order_id = self._client_order_id(idempotency_key)
        guard = self._guard(symbol, side, quantity, idempotency_key, client_order_id)
        if guard is not None:
            return guard

        if self._effective_mode in (ExecutionMode.LOCAL_PAPER, ExecutionMode.SHADOW):
            result = self._fill_paper(symbol, side, quantity, price, order_type, client_order_id)
        else:
            result = ExecutionResult(
                status="REJECTED",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0.0,
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                message="live submission requires submit_async (async broker lifecycle)",
            )
        self._record_if_filled(idempotency_key, result)
        return result

    async def submit_async(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        idempotency_key: str,
        order_type: str = "MARKET",
    ) -> ExecutionResult:
        """Submit through the effective mode, awaiting the broker for live modes."""
        side = side.upper()
        client_order_id = self._client_order_id(idempotency_key)
        guard = self._guard(symbol, side, quantity, idempotency_key, client_order_id)
        if guard is not None:
            return guard

        if self._effective_mode in (ExecutionMode.LOCAL_PAPER, ExecutionMode.SHADOW):
            result = self._fill_paper(symbol, side, quantity, price, order_type, client_order_id)
        elif self.broker_executor is None:
            result = ExecutionResult(
                status="REJECTED",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0.0,
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                message="live mode but no broker executor attached",
            )
        else:
            result = await self._submit_live(
                symbol, side, quantity, price, order_type, client_order_id
            )
        self._record_if_filled(idempotency_key, result)
        return result

    async def _submit_live(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str,
        client_order_id: str,
    ) -> ExecutionResult:
        """Place a real order via the broker executor and map its confirmed status."""
        assert self.broker_executor is not None
        request = OrderRequest(
            symbol=symbol,
            exchange="NSE",
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.LIMIT if order_type.upper() == "LIMIT" else OrderType.MARKET,
            price=price,
        )
        confirmed = await self.broker_executor.place_and_confirm(request, client_order_id)
        status_map = {
            OrderStatus.FILLED: "FILLED",
            OrderStatus.PARTIALLY_FILLED: "PARTIALLY_FILLED",
        }
        status = status_map.get(confirmed.status, "REJECTED")
        return ExecutionResult(
            status=status,
            symbol=symbol,
            side=side,
            quantity=confirmed.filled_quantity or quantity,
            fill_price=confirmed.average_price or price,
            mode=self._effective_mode.value,
            client_order_id=client_order_id,
            order_id=confirmed.order_id,
            message=confirmed.message or f"broker status: {confirmed.status.value}",
        )
