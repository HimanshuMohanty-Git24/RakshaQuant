"""
Utilities module for RakshaQuant.

Provides common utilities:
- Rate limiting for API calls
- TTL caching for expensive operations
- Error types for structured exception handling
- Circuit breaker for resilience
- Event bus for pub/sub communication
"""

from .cache import TTLCache, cached, get_news_cache, get_quote_cache, get_sentiment_cache
from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    get_broker_circuit_breaker,
    get_groq_circuit_breaker,
    get_market_data_circuit_breaker,
)
from .errors import (
    BrokerConnectionError,
    ConfigurationError,
    InsufficientFundsError,
    LLMResponseError,
    MarketDataError,
    OrderRejectedError,
    RateLimitError,
    TradingError,
    get_retry_delay,
    is_retryable_error,
)
from .events import (
    EventBus,
    EventType,
    TradingEvent,
    get_event_bus,
)
from .rate_limiter import RateLimiter, get_groq_limiter, rate_limited

__all__ = [
    # Rate limiting
    "RateLimiter",
    "rate_limited",
    "get_groq_limiter",
    # Caching
    "TTLCache",
    "cached",
    "get_news_cache",
    "get_quote_cache",
    "get_sentiment_cache",
    # Errors
    "TradingError",
    "RateLimitError",
    "LLMResponseError",
    "BrokerConnectionError",
    "OrderRejectedError",
    "InsufficientFundsError",
    "MarketDataError",
    "ConfigurationError",
    "is_retryable_error",
    "get_retry_delay",
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "get_groq_circuit_breaker",
    "get_broker_circuit_breaker",
    "get_market_data_circuit_breaker",
    # Events
    "EventBus",
    "TradingEvent",
    "EventType",
    "get_event_bus",
]
