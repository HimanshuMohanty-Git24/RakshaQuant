"""
Circuit Breaker Pattern Implementation

Prevents cascading failures by detecting failures and temporarily
blocking requests to failing services.

States:
- CLOSED: Normal operation, requests go through
- OPEN: Service failing, requests blocked immediately
- HALF_OPEN: Testing if service has recovered
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, TypeVar

from .errors import CircuitBreakerOpenError

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for external service calls.

    Prevents cascading failures by tracking failures and temporarily
    blocking requests when a service is unhealthy.

    Usage:
        breaker = CircuitBreaker(name="groq_api")

        try:
            result = breaker.call(my_function, arg1, arg2)
        except CircuitBreakerOpenError:
            # Use fallback
            result = fallback_value
    """

    name: str
    failure_threshold: int = 5  # Failures before opening
    success_threshold: int = 2  # Successes to close from half-open
    recovery_time: float = 60.0  # Seconds before trying half-open
    timeout: float = 30.0  # Request timeout

    # Internal state
    _state: CircuitState = field(default=CircuitState.CLOSED, repr=False)
    _failure_count: int = field(default=0, repr=False)
    _success_count: int = field(default=0, repr=False)
    _last_failure_time: float = field(default=0.0, repr=False)
    _last_state_change: float = field(default_factory=time.time, repr=False)

    @property
    def state(self) -> CircuitState:
        """Get current state, checking for automatic transitions."""
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self.recovery_time:
                self._transition_to(CircuitState.HALF_OPEN)
        return self._state

    @property
    def is_available(self) -> bool:
        """Check if requests can go through."""
        return self.state != CircuitState.OPEN

    def _transition_to(self, new_state: CircuitState):
        """Transition to a new state."""
        if self._state != new_state:
            logger.info(f"Circuit breaker '{self.name}': {self._state.value} -> {new_state.value}")
            self._state = new_state
            self._last_state_change = time.time()

            if new_state == CircuitState.HALF_OPEN:
                self._success_count = 0
            elif new_state == CircuitState.CLOSED:
                self._failure_count = 0

    def _record_success(self):
        """Record a successful call."""
        self._failure_count = 0

        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._transition_to(CircuitState.CLOSED)

    def _record_failure(self, error: Exception):
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        self._success_count = 0

        if self._state == CircuitState.HALF_OPEN:
            # Immediate transition back to open on failure
            self._transition_to(CircuitState.OPEN)
        elif self._failure_count >= self.failure_threshold:
            self._transition_to(CircuitState.OPEN)
            logger.warning(
                f"Circuit breaker '{self.name}' opened after {self._failure_count} failures. "
                f"Last error: {error}"
            )

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute a function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: If function fails and circuit allows
        """
        if self.state == CircuitState.OPEN:
            time_since_failure = time.time() - self._last_failure_time
            time_remaining = self.recovery_time - time_since_failure
            raise CircuitBreakerOpenError(self.name, max(0, time_remaining))

        try:
            result = func(*args, **kwargs)
            self._record_success()
            return result
        except Exception as e:
            self._record_failure(e)
            raise

    async def call_async(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute an async function with circuit breaker protection.

        Args:
            func: Async function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: If function fails
        """
        if self.state == CircuitState.OPEN:
            time_since_failure = time.time() - self._last_failure_time
            time_remaining = self.recovery_time - time_since_failure
            raise CircuitBreakerOpenError(self.name, max(0, time_remaining))

        try:
            if asyncio.iscoroutinefunction(func):
                result = await asyncio.wait_for(func(*args, **kwargs), timeout=self.timeout)
            else:
                result = func(*args, **kwargs)

            self._record_success()
            return result
        except TimeoutError as e:
            self._record_failure(e)
            raise
        except Exception as e:
            self._record_failure(e)
            raise

    def reset(self):
        """Manually reset the circuit breaker to closed state."""
        self._transition_to(CircuitState.CLOSED)
        self._failure_count = 0
        self._success_count = 0
        logger.info(f"Circuit breaker '{self.name}' manually reset")

    def get_stats(self) -> dict[str, Any]:
        """Get circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time,
            "time_in_state": time.time() - self._last_state_change,
        }


# ===========================================
# Global Circuit Breakers
# ===========================================

_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_time: float = 60.0,
) -> CircuitBreaker:
    """Get or create a named circuit breaker."""
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_time=recovery_time,
        )
    return _circuit_breakers[name]


def get_groq_circuit_breaker() -> CircuitBreaker:
    """Get circuit breaker for Groq API."""
    return get_circuit_breaker("groq_api", failure_threshold=3, recovery_time=30.0)


def get_broker_circuit_breaker() -> CircuitBreaker:
    """Get circuit breaker for broker API."""
    return get_circuit_breaker("broker_api", failure_threshold=5, recovery_time=60.0)


def get_market_data_circuit_breaker() -> CircuitBreaker:
    """Get circuit breaker for market data feed."""
    return get_circuit_breaker("market_data", failure_threshold=5, recovery_time=30.0)


# ===========================================
# Decorator
# ===========================================


def with_circuit_breaker(
    breaker_name: str | None = None,
    breaker: CircuitBreaker | None = None,
    fallback: Callable | None = None,
) -> Callable[[F], F]:
    """
    Decorator to wrap a function with circuit breaker protection.

    Args:
        breaker_name: Name of circuit breaker to use/create
        breaker: Existing circuit breaker instance
        fallback: Fallback function to call if circuit is open

    Example:
        @with_circuit_breaker("groq_api", fallback=lambda: default_response)
        def call_llm(prompt):
            ...
    """

    def decorator(func: F) -> F:
        cb = breaker or get_circuit_breaker(breaker_name or func.__name__)

        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await cb.call_async(func, *args, **kwargs)
                except CircuitBreakerOpenError:
                    if fallback:
                        if asyncio.iscoroutinefunction(fallback):
                            return await fallback(*args, **kwargs)
                        return fallback(*args, **kwargs)
                    raise

            return async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    return cb.call(func, *args, **kwargs)
                except CircuitBreakerOpenError:
                    if fallback:
                        return fallback(*args, **kwargs)
                    raise

            return sync_wrapper

    return decorator


# ===========================================
# Health Check
# ===========================================


def get_all_circuit_breaker_stats() -> dict[str, dict[str, Any]]:
    """Get statistics for all circuit breakers."""
    return {name: cb.get_stats() for name, cb in _circuit_breakers.items()}


def reset_all_circuit_breakers():
    """Reset all circuit breakers to closed state."""
    for cb in _circuit_breakers.values():
        cb.reset()
