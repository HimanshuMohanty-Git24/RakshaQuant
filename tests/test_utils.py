import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.utils.cache import (
    TTLCache,
    _caches,
    cached,
    get_cache,
    get_discovery_cache,
    get_news_cache,
    get_quote_cache,
    get_sentiment_cache,
)
from src.utils.rate_limiter import RateLimiter, get_groq_limiter, rate_limited

# --- RateLimiter Tests ---


@pytest.fixture
def rate_limiter():
    return RateLimiter(requests_per_minute=60)  # 1 request per second


def test_rate_limiter_init(rate_limiter):
    assert rate_limiter.requests_per_minute == 60
    assert rate_limiter.tokens_per_second == 1.0
    assert rate_limiter._tokens == 60.0


def test_rate_limiter_refill(rate_limiter):
    rate_limiter._tokens = 0.0
    rate_limiter._last_refill = time.monotonic() - 1.0  # 1 second ago

    rate_limiter._refill_tokens()

    # Should have added 1 token
    assert abs(rate_limiter._tokens - 1.0) < 0.1

    # Check capping
    rate_limiter._tokens = 59.0
    rate_limiter._last_refill = time.monotonic() - 10.0  # 10 seconds ago
    rate_limiter._refill_tokens()
    assert rate_limiter._tokens == 60.0


@pytest.mark.asyncio
async def test_rate_limiter_acquire_async(rate_limiter):
    rate_limiter._tokens = 1.0
    assert await rate_limiter.acquire() is True
    assert rate_limiter._tokens < 1.0

    # Test waiting
    rate_limiter._tokens = 0.0
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        assert await rate_limiter.acquire() is True
        mock_sleep.assert_called_once()


def test_rate_limiter_acquire_sync(rate_limiter):
    rate_limiter._tokens = 1.0
    assert rate_limiter.acquire_sync() is True
    assert rate_limiter._tokens < 1.0

    # Test waiting
    rate_limiter._tokens = 0.0
    with patch("time.sleep") as mock_sleep:
        assert rate_limiter.acquire_sync() is True
        mock_sleep.assert_called_once()


def test_rate_limiter_get_wait_time(rate_limiter):
    rate_limiter._tokens = 1.0
    assert rate_limiter.get_wait_time() == 0.0

    rate_limiter._tokens = 0.5
    # Needed 0.5 more, at 1 token/sec => 0.5 sec
    assert abs(rate_limiter.get_wait_time() - 0.5) < 0.01


def test_get_groq_limiter():
    # Reset global
    import src.utils.rate_limiter

    src.utils.rate_limiter._groq_limiter = None

    limiter1 = get_groq_limiter()
    limiter2 = get_groq_limiter()
    assert limiter1 is limiter2
    assert limiter1.requests_per_minute == 30


@pytest.mark.asyncio
async def test_rate_limited_decorator_async():
    limiter = RateLimiter(requests_per_minute=60)
    limiter.acquire = AsyncMock(return_value=True)

    @rate_limited(limiter=limiter)
    async def my_func():
        return "success"

    assert await my_func() == "success"
    limiter.acquire.assert_called_once()


def test_rate_limited_decorator_sync():
    limiter = RateLimiter(requests_per_minute=60)
    limiter.acquire_sync = MagicMock(return_value=True)

    @rate_limited(limiter=limiter)
    def my_func():
        return "success"

    assert my_func() == "success"
    limiter.acquire_sync.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limited_retry_async():
    limiter = RateLimiter(requests_per_minute=60)

    mock_func = AsyncMock()
    mock_func.side_effect = [Exception("Rate limit exceeded"), "success"]

    @rate_limited(limiter=limiter, max_retries=2, backoff_factor=1.0)
    async def my_func():
        return await mock_func()

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        assert await my_func() == "success"
        assert mock_func.call_count == 2
        mock_sleep.assert_called_once()


def test_rate_limited_retry_sync():
    limiter = RateLimiter(requests_per_minute=60)

    mock_func = MagicMock()
    mock_func.side_effect = [Exception("Rate limit exceeded"), "success"]

    @rate_limited(limiter=limiter, max_retries=2, backoff_factor=1.0)
    def my_func():
        return mock_func()

    with patch("time.sleep") as mock_sleep:
        assert my_func() == "success"
        assert mock_func.call_count == 2
        mock_sleep.assert_called_once()


# --- TTLCache Tests ---


@pytest.fixture
def ttl_cache():
    return TTLCache(default_ttl=10, max_size=5, cleanup_interval=1)


def test_cache_set_get(ttl_cache):
    ttl_cache.set("key1", "value1")
    assert ttl_cache.get("key1") == "value1"

    assert ttl_cache.get("non_existent") is None


def test_cache_expiration(ttl_cache):
    ttl_cache.set("key1", "value1", ttl=0.1)
    time.sleep(0.2)
    assert ttl_cache.get("key1") is None


def test_cache_eviction(ttl_cache):
    # Fill cache
    for i in range(5):
        ttl_cache.set(f"key{i}", i)

    assert len(ttl_cache._cache) == 5

    # Add one more
    ttl_cache.set("key5", 5)

    # Should have evicted some (implementation removes 10% or at least 1)
    # 5 // 10 = 0 -> max(1, 0) = 1 removed.
    # So size should be 5 again (4 old + 1 new)
    assert len(ttl_cache._cache) < 6


def test_cache_cleanup(ttl_cache):
    ttl_cache.set("key1", "value1", ttl=0.1)
    ttl_cache._last_cleanup = time.monotonic() - 2.0

    time.sleep(0.2)
    # Trigger cleanup via get or internal
    ttl_cache._cleanup()

    assert "key1" not in ttl_cache._cache


def test_cache_delete_clear(ttl_cache):
    ttl_cache.set("key1", "value1")
    assert ttl_cache.delete("key1") is True
    assert ttl_cache.get("key1") is None
    assert ttl_cache.delete("key1") is False

    ttl_cache.set("key2", "value2")
    ttl_cache.clear()
    assert ttl_cache.get("key2") is None
    assert len(ttl_cache._cache) == 0


def test_cache_stats(ttl_cache):
    ttl_cache.set("key1", "value1")
    ttl_cache.get("key1")  # Hit
    ttl_cache.get("key2")  # Miss

    stats = ttl_cache.get_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["entries"] == 1


def test_get_caches():
    # Reset global
    _caches.clear()

    c1 = get_cache("test")
    c2 = get_cache("test")
    assert c1 is c2

    assert get_news_cache().default_ttl == 300
    assert get_quote_cache().default_ttl == 60
    assert get_sentiment_cache().default_ttl == 600
    assert get_discovery_cache().default_ttl == 900


@pytest.mark.asyncio
async def test_cached_decorator_async():
    _caches.clear()

    @cached(cache="test_async", ttl=10)
    async def my_func(x):
        return x * 2

    # First call
    assert await my_func(2) == 4

    # Check cache
    cache = get_cache("test_async")
    assert cache.hits == 0  # First was miss

    # Second call (should hit cache)
    assert await my_func(2) == 4
    assert cache.hits == 1


def test_cached_decorator_sync():
    _caches.clear()

    @cached(cache="test_sync", ttl=10)
    def my_func(x):
        return x * 2

    # First call
    assert my_func(2) == 4

    # Check cache
    cache = get_cache("test_sync")
    assert cache.hits == 0

    # Second call
    assert my_func(2) == 4
    assert cache.hits == 1


def test_cached_decorator_with_instance():
    cache = TTLCache()

    @cached(cache=cache)
    def my_func():
        return "val"

    my_func()
    assert len(cache._cache) == 1
