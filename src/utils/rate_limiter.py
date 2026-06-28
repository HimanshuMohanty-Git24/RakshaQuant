"""
Rate Limiter Module

Implements token bucket algorithm to prevent hitting API rate limits.
Designed for Groq API (30 requests/minute free tier).

Features:
- Token bucket rate limiting
- Exponential backoff on failures
- Async-compatible
- Decorator for easy use
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class RateLimiter:
    """
    Token bucket rate limiter.

    Default: 30 requests per minute = 0.5 requests per second.
    This matches Groq's free tier limits.
    """

    requests_per_minute: int = 30
    max_retries: int = 3
    base_backoff: float = 1.0

    # Internal state
    _tokens: float = field(default=0.0, repr=False)
    _last_refill: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self):
        self._tokens = float(self.requests_per_minute)
        self._last_refill = time.monotonic()

    @property
    def tokens_per_second(self) -> float:
        return self.requests_per_minute / 60.0

    def _refill_tokens(self):
        """Refill tokens based on time elapsed."""
        now = time.monotonic()
        elapsed = now - self._last_refill

        # Add tokens based on elapsed time
        tokens_to_add = elapsed * self.tokens_per_second
        self._tokens = min(
            float(self.requests_per_minute),  # Cap at max
            self._tokens + tokens_to_add,
        )
        self._last_refill = now

    async def acquire(self) -> bool:
        """
        Acquire a token for making a request.

        Blocks until a token is available.

        Returns:
            True when token acquired
        """
        async with self._lock:
            self._refill_tokens()

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True

            # Calculate wait time for next token
            tokens_needed = 1.0 - self._tokens
            wait_time = tokens_needed / self.tokens_per_second

            logger.debug(f"Rate limited. Waiting {wait_time:.2f}s for token...")
            await asyncio.sleep(wait_time)

            self._refill_tokens()
            self._tokens -= 1.0
            return True

    def acquire_sync(self) -> bool:
        """Synchronous version of acquire."""
        self._refill_tokens()

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True

        # Calculate wait time
        tokens_needed = 1.0 - self._tokens
        wait_time = tokens_needed / self.tokens_per_second

        logger.debug(f"Rate limited. Waiting {wait_time:.2f}s for token...")
        time.sleep(wait_time)

        self._refill_tokens()
        self._tokens -= 1.0
        return True

    def get_wait_time(self) -> float:
        """Get estimated wait time until a token is available."""
        self._refill_tokens()

        if self._tokens >= 1.0:
            return 0.0

        tokens_needed = 1.0 - self._tokens
        return tokens_needed / self.tokens_per_second

    @property
    def available_tokens(self) -> float:
        """Get current available tokens."""
        self._refill_tokens()
        return self._tokens


# Global rate limiter instance for Groq API
_groq_limiter: RateLimiter | None = None


def get_groq_limiter() -> RateLimiter:
    """Get or create the global Groq rate limiter."""
    global _groq_limiter
    if _groq_limiter is None:
        _groq_limiter = RateLimiter(
            requests_per_minute=30,  # Groq free tier
            max_retries=3,
            base_backoff=2.0,
        )
    return _groq_limiter


def rate_limited(
    limiter: RateLimiter | None = None,
    max_retries: int = 3,
    backoff_factor: float = 2.0,
) -> Callable[[F], F]:
    """
    Decorator to rate limit a function.

    Args:
        limiter: RateLimiter instance (uses Groq limiter by default)
        max_retries: Maximum retry attempts on rate limit errors
        backoff_factor: Multiplier for exponential backoff

    Example:
        @rate_limited()
        async def call_groq_api():
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            _limiter = limiter or get_groq_limiter()

            for attempt in range(max_retries + 1):
                await _limiter.acquire()

                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    error_str = str(e).lower()

                    # Check if it's a rate limit error
                    if "rate" in error_str and "limit" in error_str:
                        if attempt < max_retries:
                            wait_time = backoff_factor**attempt
                            logger.warning(
                                f"Rate limit hit. Retrying in {wait_time:.1f}s "
                                f"(attempt {attempt + 1}/{max_retries})"
                            )
                            await asyncio.sleep(wait_time)
                            continue

                    # Re-raise non-rate-limit errors or if retries exhausted
                    raise

            # Should not reach here, but just in case
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            _limiter = limiter or get_groq_limiter()

            for attempt in range(max_retries + 1):
                _limiter.acquire_sync()

                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    error_str = str(e).lower()

                    if "rate" in error_str and "limit" in error_str:
                        if attempt < max_retries:
                            wait_time = backoff_factor**attempt
                            logger.warning(
                                f"Rate limit hit. Retrying in {wait_time:.1f}s "
                                f"(attempt {attempt + 1}/{max_retries})"
                            )
                            time.sleep(wait_time)
                            continue

                    raise

            return func(*args, **kwargs)

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore

    return decorator


def test_rate_limiter():
    """Test the rate limiter."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[RATE LIMITER] RakshaQuant - Rate Limiter Test")
    print("=" * 60)

    # Create limiter with 10 RPM for faster testing
    limiter = RateLimiter(requests_per_minute=10)

    print(f"\n[CONFIG] Requests per minute: {limiter.requests_per_minute}")
    print(f"[CONFIG] Tokens per second: {limiter.tokens_per_second:.3f}")

    print("\n[TEST] Making 5 rapid requests...")

    for i in range(5):
        start = time.monotonic()
        limiter.acquire_sync()
        elapsed = (time.monotonic() - start) * 1000

        print(
            f"  Request {i + 1}: acquired in {elapsed:.1f}ms | tokens: {limiter.available_tokens:.1f}"
        )

    print("\n[TEST] Tokens depleted, next request will wait...")

    wait_time = limiter.get_wait_time()
    print(f"  Estimated wait: {wait_time:.2f}s")

    start = time.monotonic()
    limiter.acquire_sync()
    elapsed = time.monotonic() - start
    print(f"  Request 6: acquired after {elapsed:.2f}s wait")

    print("\n" + "=" * 60)
    print("[SUCCESS] Rate limiter working!")
    print("=" * 60)


if __name__ == "__main__":
    test_rate_limiter()
