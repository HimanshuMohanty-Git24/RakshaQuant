"""
Custom Exception Types Module

Structured exception hierarchy for the trading system.
Provides clear error categorization for proper handling and recovery.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class TradingError(Exception):
    """Base exception for all trading system errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.timestamp = datetime.now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }


# ===========================================
# LLM/API Errors
# ===========================================


class LLMError(TradingError):
    """Base class for LLM-related errors."""

    pass


class RateLimitError(LLMError):
    """API rate limit exceeded."""

    def __init__(self, provider: str = "groq", retry_after: float = 60.0):
        super().__init__(
            f"{provider} API rate limit exceeded. Retry after {retry_after}s",
            {"provider": provider, "retry_after": retry_after},
        )
        self.retry_after = retry_after


class LLMResponseError(LLMError):
    """LLM returned invalid or unparseable response."""

    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message, {"raw_response": raw_response[:500]})


class LLMTimeoutError(LLMError):
    """LLM request timed out."""

    def __init__(self, timeout: float):
        super().__init__(f"LLM request timed out after {timeout}s", {"timeout": timeout})


# ===========================================
# Broker/Execution Errors
# ===========================================


class BrokerError(TradingError):
    """Base class for broker-related errors."""

    pass


class BrokerConnectionError(BrokerError):
    """Failed to connect to broker API."""

    def __init__(self, broker: str = "dhan", reason: str = ""):
        super().__init__(
            f"Failed to connect to {broker}: {reason}", {"broker": broker, "reason": reason}
        )


class OrderRejectedError(BrokerError):
    """Order was rejected by the broker."""

    def __init__(self, order_id: str, symbol: str, reason: str):
        super().__init__(
            f"Order {order_id} for {symbol} rejected: {reason}",
            {"order_id": order_id, "symbol": symbol, "reason": reason},
        )


class InsufficientFundsError(BrokerError):
    """Insufficient funds for the order."""

    def __init__(self, required: float, available: float, symbol: str):
        super().__init__(
            f"Insufficient funds for {symbol}: need ₹{required:,.2f}, have ₹{available:,.2f}",
            {"required": required, "available": available, "symbol": symbol},
        )


class PositionNotFoundError(BrokerError):
    """Trying to close a position that doesn't exist."""

    def __init__(self, symbol: str):
        super().__init__(f"No open position found for {symbol}", {"symbol": symbol})


# ===========================================
# Market Data Errors
# ===========================================


class MarketDataError(TradingError):
    """Base class for market data errors."""

    pass


class DataFeedConnectionError(MarketDataError):
    """Failed to connect to market data feed."""

    def __init__(self, source: str, reason: str = ""):
        super().__init__(
            f"Failed to connect to {source} data feed: {reason}",
            {"source": source, "reason": reason},
        )


class SymbolNotFoundError(MarketDataError):
    """Symbol not found in market data."""

    def __init__(self, symbol: str):
        super().__init__(f"Symbol not found: {symbol}", {"symbol": symbol})


class InsufficientDataError(MarketDataError):
    """Not enough historical data for analysis."""

    def __init__(self, symbol: str, required: int, available: int):
        super().__init__(
            f"Insufficient data for {symbol}: need {required} bars, have {available}",
            {"symbol": symbol, "required": required, "available": available},
        )


class MarketClosedError(MarketDataError):
    """Market is closed, cannot get live data."""

    def __init__(self):
        super().__init__("Market is currently closed")


# ===========================================
# Agent/Workflow Errors
# ===========================================


class AgentError(TradingError):
    """Base class for agent-related errors."""

    pass


class AgentTimeoutError(AgentError):
    """Agent workflow timed out."""

    def __init__(self, agent_name: str, timeout: float):
        super().__init__(
            f"Agent {agent_name} timed out after {timeout}s",
            {"agent": agent_name, "timeout": timeout},
        )


class WorkflowError(AgentError):
    """Error in agent workflow execution."""

    def __init__(self, workflow_id: str, stage: str, reason: str):
        super().__init__(
            f"Workflow {workflow_id} failed at {stage}: {reason}",
            {"workflow_id": workflow_id, "stage": stage, "reason": reason},
        )


class ValidationError(AgentError):
    """Signal or trade validation failed."""

    def __init__(self, signal_id: str, reason: str):
        super().__init__(
            f"Validation failed for signal {signal_id}: {reason}",
            {"signal_id": signal_id, "reason": reason},
        )


# ===========================================
# Risk/Compliance Errors
# ===========================================


class RiskError(TradingError):
    """Base class for risk-related errors."""

    pass


class KillSwitchTriggeredError(RiskError):
    """Trading kill switch has been triggered."""

    def __init__(self, reason: str, daily_loss: float, limit: float):
        super().__init__(
            f"Kill switch triggered: {reason}",
            {"reason": reason, "daily_loss": daily_loss, "limit": limit},
        )


class RiskLimitExceededError(RiskError):
    """A risk limit has been exceeded."""

    def __init__(self, limit_type: str, current_value: float, max_value: float):
        super().__init__(
            f"Risk limit exceeded: {limit_type} ({current_value} > {max_value})",
            {"limit_type": limit_type, "current": current_value, "max": max_value},
        )


class MaxPositionsExceededError(RiskError):
    """Maximum number of positions reached."""

    def __init__(self, current: int, max_positions: int):
        super().__init__(
            f"Max positions exceeded: {current} >= {max_positions}",
            {"current": current, "max": max_positions},
        )


# ===========================================
# Database/Memory Errors
# ===========================================


class DatabaseError(TradingError):
    """Base class for database errors."""

    pass


class DatabaseConnectionError(DatabaseError):
    """Failed to connect to database."""

    def __init__(self, url: str, reason: str = ""):
        # Mask credentials in URL
        masked_url = url.split("@")[-1] if "@" in url else url
        super().__init__(
            f"Failed to connect to database: {reason}", {"url": masked_url, "reason": reason}
        )


class LessonStorageError(DatabaseError):
    """Failed to store or retrieve lesson."""

    def __init__(self, operation: str, lesson_id: str = "", reason: str = ""):
        super().__init__(
            f"Lesson {operation} failed for {lesson_id}: {reason}",
            {"operation": operation, "lesson_id": lesson_id, "reason": reason},
        )


# ===========================================
# Configuration Errors
# ===========================================


class ConfigurationError(TradingError):
    """Configuration is invalid or missing."""

    def __init__(self, setting: str, reason: str):
        super().__init__(
            f"Configuration error for {setting}: {reason}", {"setting": setting, "reason": reason}
        )


# ===========================================
# Circuit Breaker Error
# ===========================================


class CircuitBreakerOpenError(TradingError):
    """Circuit breaker is open, service unavailable."""

    def __init__(self, service: str, recovery_time: float):
        super().__init__(
            f"Service {service} unavailable (circuit breaker open). Retry in {recovery_time}s",
            {"service": service, "recovery_time": recovery_time},
        )


# ===========================================
# Error Recovery Helpers
# ===========================================


@dataclass
class ErrorContext:
    """Context for error handling and recovery."""

    error: TradingError
    recoverable: bool = True
    retry_count: int = 0
    max_retries: int = 3
    fallback_available: bool = False
    fallback_result: Any = None

    def should_retry(self) -> bool:
        """Check if we should retry the operation."""
        return self.recoverable and self.retry_count < self.max_retries

    def increment_retry(self) -> "ErrorContext":
        """Increment retry counter."""
        self.retry_count += 1
        return self


def is_retryable_error(error: Exception) -> bool:
    """Check if an error is retryable."""
    retryable_types = (
        RateLimitError,
        LLMTimeoutError,
        DataFeedConnectionError,
        BrokerConnectionError,
    )
    return isinstance(error, retryable_types)


def get_retry_delay(error: Exception, attempt: int) -> float:
    """Get delay before retry based on error type and attempt."""
    base_delay = 1.0

    if isinstance(error, RateLimitError):
        return error.retry_after

    # Exponential backoff with jitter
    import random

    delay = base_delay * (2**attempt)
    jitter = random.uniform(0, delay * 0.1)
    return min(delay + jitter, 60.0)  # Max 60 seconds
