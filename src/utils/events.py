"""
Event Bus Module

Provides event-driven communication between system components.
Enables loose coupling and real-time updates.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of events in the trading system."""

    # Market events
    QUOTE_UPDATE = "quote_update"
    INDICATOR_UPDATE = "indicator_update"
    SIGNAL_GENERATED = "signal_generated"

    # Agent events
    REGIME_CLASSIFIED = "regime_classified"
    STRATEGY_SELECTED = "strategy_selected"
    SIGNAL_VALIDATED = "signal_validated"
    SIGNAL_REJECTED = "signal_rejected"
    TRADE_APPROVED = "trade_approved"
    TRADE_REJECTED = "trade_rejected"

    # Execution events
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"

    # System events
    CYCLE_STARTED = "cycle_started"
    CYCLE_COMPLETED = "cycle_completed"
    ERROR_OCCURRED = "error_occurred"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"

    # Memory events
    LESSON_LEARNED = "lesson_learned"
    LESSON_APPLIED = "lesson_applied"


@dataclass
class TradingEvent:
    """A trading system event."""

    event_type: EventType
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "system"
    correlation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "correlation_id": self.correlation_id,
        }


# Type alias for event handlers
EventHandler = Callable[[TradingEvent], Coroutine[Any, Any, None]]
SyncEventHandler = Callable[[TradingEvent], None]


class EventBus:
    """
    Async event bus for trading system components.

    Supports:
    - Pub/sub pattern with typed events
    - Async and sync handlers
    - Event filtering
    - Event history

    Usage:
        bus = EventBus()

        async def on_trade(event):
            print(f"Trade: {event.data}")

        bus.subscribe(EventType.TRADE_APPROVED, on_trade)
        await bus.publish(TradingEvent(EventType.TRADE_APPROVED, {...}))
    """

    def __init__(self, max_history: int = 100):
        """
        Initialize event bus.

        Args:
            max_history: Maximum events to keep in history
        """
        self._async_handlers: dict[EventType, list[EventHandler]] = {}
        self._sync_handlers: dict[EventType, list[SyncEventHandler]] = {}
        self._history: list[TradingEvent] = []
        self._max_history = max_history
        self._lock = asyncio.Lock()

    def subscribe(
        self,
        event_type: EventType,
        handler: EventHandler | SyncEventHandler,
    ) -> None:
        """
        Subscribe to an event type.

        Args:
            event_type: Type of event to subscribe to
            handler: Async or sync handler function
        """
        if asyncio.iscoroutinefunction(handler):
            if event_type not in self._async_handlers:
                self._async_handlers[event_type] = []
            self._async_handlers[event_type].append(handler)
        else:
            if event_type not in self._sync_handlers:
                self._sync_handlers[event_type] = []
            self._sync_handlers[event_type].append(handler)

        logger.debug(f"Subscribed handler to {event_type.value}")

    def unsubscribe(
        self,
        event_type: EventType,
        handler: EventHandler | SyncEventHandler,
    ) -> None:
        """Unsubscribe a handler from an event type."""
        if asyncio.iscoroutinefunction(handler):
            if event_type in self._async_handlers:
                try:
                    self._async_handlers[event_type].remove(handler)
                except ValueError:
                    pass
        else:
            if event_type in self._sync_handlers:
                try:
                    self._sync_handlers[event_type].remove(handler)
                except ValueError:
                    pass

    async def publish(self, event: TradingEvent) -> None:
        """
        Publish an event to all subscribers.

        Args:
            event: Event to publish
        """
        async with self._lock:
            # Add to history
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history :]

        # Call sync handlers
        sync_handlers = self._sync_handlers.get(event.event_type, [])
        for handler in sync_handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Sync handler error for {event.event_type.value}: {e}")

        # Call async handlers concurrently
        async_handlers = self._async_handlers.get(event.event_type, [])
        if async_handlers:
            tasks = [handler(event) for handler in async_handlers]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for handler, result in zip(async_handlers, results):
                if isinstance(result, Exception):
                    logger.error(f"Async handler error for {event.event_type.value}: {result}")

    def publish_sync(self, event: TradingEvent) -> None:
        """
        Publish an event synchronously (only calls sync handlers).

        Args:
            event: Event to publish
        """
        # Add to history
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        # Call sync handlers only
        sync_handlers = self._sync_handlers.get(event.event_type, [])
        for handler in sync_handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Sync handler error for {event.event_type.value}: {e}")

    def get_history(
        self,
        event_type: EventType | None = None,
        limit: int = 50,
    ) -> list[TradingEvent]:
        """
        Get event history.

        Args:
            event_type: Filter by event type (None for all)
            limit: Maximum events to return

        Returns:
            List of events (newest first)
        """
        events = self._history

        if event_type:
            events = [e for e in events if e.event_type == event_type]

        return list(reversed(events[-limit:]))

    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()


# ===========================================
# Global Event Bus
# ===========================================

_global_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get or create the global event bus."""
    global _global_event_bus
    if _global_event_bus is None:
        _global_event_bus = EventBus()
    return _global_event_bus


# ===========================================
# Event Publishers (Convenience Functions)
# ===========================================


async def emit_quote_update(symbol: str, quote: dict[str, Any]) -> None:
    """Emit a quote update event."""
    await get_event_bus().publish(
        TradingEvent(
            event_type=EventType.QUOTE_UPDATE,
            data={"symbol": symbol, "quote": quote},
            source="market_data",
        )
    )


async def emit_signal_generated(signal: dict[str, Any]) -> None:
    """Emit a signal generated event."""
    await get_event_bus().publish(
        TradingEvent(
            event_type=EventType.SIGNAL_GENERATED,
            data=signal,
            source="signal_engine",
        )
    )


async def emit_trade_approved(trade: dict[str, Any], workflow_id: str = "") -> None:
    """Emit a trade approved event."""
    await get_event_bus().publish(
        TradingEvent(
            event_type=EventType.TRADE_APPROVED,
            data=trade,
            source="risk_agent",
            correlation_id=workflow_id,
        )
    )


async def emit_order_filled(order: dict[str, Any]) -> None:
    """Emit an order filled event."""
    await get_event_bus().publish(
        TradingEvent(
            event_type=EventType.ORDER_FILLED,
            data=order,
            source="execution",
        )
    )


async def emit_error(error: Exception, context: dict[str, Any] = None) -> None:
    """Emit an error event."""
    await get_event_bus().publish(
        TradingEvent(
            event_type=EventType.ERROR_OCCURRED,
            data={
                "error_type": type(error).__name__,
                "message": str(error),
                "context": context or {},
            },
            source="system",
        )
    )


async def emit_cycle_completed(
    workflow_id: str,
    signals_count: int,
    approved_count: int,
    rejected_count: int,
) -> None:
    """Emit a cycle completed event."""
    await get_event_bus().publish(
        TradingEvent(
            event_type=EventType.CYCLE_COMPLETED,
            data={
                "workflow_id": workflow_id,
                "signals_count": signals_count,
                "approved_count": approved_count,
                "rejected_count": rejected_count,
            },
            source="workflow",
            correlation_id=workflow_id,
        )
    )


# ===========================================
# Event Subscriber Decorators
# ===========================================


def on_event(event_type: EventType):
    """
    Decorator to register a function as an event handler.

    Usage:
        @on_event(EventType.TRADE_APPROVED)
        async def handle_trade(event: TradingEvent):
            print(f"Trade approved: {event.data}")
    """

    def decorator(func):
        get_event_bus().subscribe(event_type, func)
        return func

    return decorator
