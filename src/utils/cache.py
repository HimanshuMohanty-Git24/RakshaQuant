"""
TTL Cache Module

In-memory cache with time-to-live (TTL) for expensive operations.
Reduces redundant API calls for news, quotes, and sentiment.

Features:
- Configurable TTL per entry
- Automatic expiration
- Thread-safe
- Decorator for easy use
"""

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class CacheEntry:
    """A cached value with expiration."""

    value: Any
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class TTLCache:
    """
    In-memory cache with time-to-live.

    Usage:
        cache = TTLCache(default_ttl=300)  # 5 minutes
        cache.set("key", "value")
        value = cache.get("key")
    """

    def __init__(
        self,
        default_ttl: int = 300,
        max_size: int = 1000,
        cleanup_interval: int = 60,
    ):
        """
        Initialize cache.

        Args:
            default_ttl: Default time-to-live in seconds
            max_size: Maximum cache entries
            cleanup_interval: Seconds between automatic cleanups
        """
        self.default_ttl = default_ttl
        self.max_size = max_size
        self.cleanup_interval = cleanup_interval

        self._cache: dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._last_cleanup = time.monotonic()

        # Stats
        self.hits = 0
        self.misses = 0

    def _cleanup(self):
        """Remove expired entries."""
        now = time.monotonic()

        if now - self._last_cleanup < self.cleanup_interval:
            return

        expired_keys = [key for key, entry in self._cache.items() if entry.is_expired]

        for key in expired_keys:
            del self._cache[key]

        self._last_cleanup = now

        if expired_keys:
            logger.debug(f"Cache cleanup: removed {len(expired_keys)} expired entries")

    def _evict_if_needed(self):
        """Evict oldest entries if cache is full."""
        if len(self._cache) < self.max_size:
            return

        # Remove 10% of entries (oldest first)
        entries = sorted(self._cache.items(), key=lambda x: x[1].expires_at)

        to_remove = max(1, len(entries) // 10)
        for key, _ in entries[:to_remove]:
            del self._cache[key]

        logger.debug(f"Cache eviction: removed {to_remove} entries")

    def get(self, key: str) -> Any | None:
        """
        Get a value from cache.

        Returns None if not found or expired.
        """
        with self._lock:
            self._cleanup()

            entry = self._cache.get(key)

            if entry is None:
                self.misses += 1
                return None

            if entry.is_expired:
                del self._cache[key]
                self.misses += 1
                return None

            self.hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: int | None = None):
        """
        Set a value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (uses default if None)
        """
        with self._lock:
            self._evict_if_needed()

            ttl = ttl or self.default_ttl
            expires_at = time.monotonic() + ttl

            self._cache[key] = CacheEntry(value=value, expires_at=expires_at)

    def delete(self, key: str) -> bool:
        """Delete a key from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self):
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            logger.info("Cache cleared")

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self.hits + self.misses
            hit_rate = (self.hits / total * 100) if total > 0 else 0

            return {
                "entries": len(self._cache),
                "max_size": self.max_size,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": f"{hit_rate:.1f}%",
            }


# Global caches for different data types
_caches: dict[str, TTLCache] = {}


def get_cache(name: str, default_ttl: int = 300) -> TTLCache:
    """Get or create a named cache."""
    if name not in _caches:
        _caches[name] = TTLCache(default_ttl=default_ttl)
    return _caches[name]


# Pre-configured caches
def get_news_cache() -> TTLCache:
    """Cache for news data (5 minute TTL)."""
    return get_cache("news", default_ttl=300)


def get_quote_cache() -> TTLCache:
    """Cache for quote data (1 minute TTL)."""
    return get_cache("quotes", default_ttl=60)


def get_sentiment_cache() -> TTLCache:
    """Cache for sentiment data (10 minute TTL)."""
    return get_cache("sentiment", default_ttl=600)


def get_discovery_cache() -> TTLCache:
    """Cache for discovery results (15 minute TTL)."""
    return get_cache("discovery", default_ttl=900)


def _make_cache_key(func: Callable, args: tuple, kwargs: dict) -> str:
    """Generate a cache key from function and arguments."""
    key_parts = [
        func.__module__,
        func.__name__,
        str(args),
        json.dumps(kwargs, sort_keys=True, default=str),
    ]
    key_str = "|".join(key_parts)
    return hashlib.md5(key_str.encode()).hexdigest()


def cached(
    cache: TTLCache | str | None = None,
    ttl: int | None = None,
) -> Callable[[F], F]:
    """
    Decorator to cache function results.

    Args:
        cache: TTLCache instance or cache name string
        ttl: Optional TTL override

    Example:
        @cached("news", ttl=300)
        async def fetch_news(symbol):
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Get cache instance
            if isinstance(cache, TTLCache):
                _cache = cache
            elif isinstance(cache, str):
                _cache = get_cache(cache)
            else:
                _cache = get_cache("default")

            # Generate key
            key = _make_cache_key(func, args, kwargs)

            # Try cache
            result = _cache.get(key)
            if result is not None:
                logger.debug(f"Cache hit for {func.__name__}")
                return result

            # Call function
            result = await func(*args, **kwargs)

            # Store in cache
            _cache.set(key, result, ttl)
            return result

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            if isinstance(cache, TTLCache):
                _cache = cache
            elif isinstance(cache, str):
                _cache = get_cache(cache)
            else:
                _cache = get_cache("default")

            key = _make_cache_key(func, args, kwargs)

            result = _cache.get(key)
            if result is not None:
                logger.debug(f"Cache hit for {func.__name__}")
                return result

            result = func(*args, **kwargs)
            _cache.set(key, result, ttl)
            return result

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore

    return decorator


def test_cache():
    """Test the cache."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[CACHE] RakshaQuant - TTL Cache Test")
    print("=" * 60)

    cache = TTLCache(default_ttl=2)  # 2 second TTL for testing

    print("\n[TEST] Setting values...")
    cache.set("key1", "value1")
    cache.set("key2", {"data": [1, 2, 3]})
    cache.set("key3", "short_lived", ttl=1)

    print(f"  key1: {cache.get('key1')}")
    print(f"  key2: {cache.get('key2')}")
    print(f"  key3: {cache.get('key3')}")

    print(f"\n[STATS] {cache.get_stats()}")

    print("\n[TEST] Waiting 1.5 seconds...")
    time.sleep(1.5)

    print(f"  key1: {cache.get('key1')} (still valid)")
    print(f"  key3: {cache.get('key3')} (expired)")

    print("\n[TEST] Waiting 1 more second...")
    time.sleep(1)

    print(f"  key1: {cache.get('key1')} (expired)")

    print(f"\n[STATS] {cache.get_stats()}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Cache working!")
    print("=" * 60)


if __name__ == "__main__":
    test_cache()
