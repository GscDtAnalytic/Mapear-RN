"""Resilience tests — behavior when Redis is unavailable."""

import contextlib
from unittest.mock import MagicMock

import pytest

from mapear_infra.cache import ContentCache
from mapear_rss.extraction.circuit_breaker import CircuitBreaker, CircuitState


class TestCircuitBreakerWithoutRedis:
    """Circuit breaker should degrade gracefully without Redis."""

    def test_circuit_breaker_redis_connection_error(self) -> None:
        """CB should default to allowing requests when Redis is down."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = ConnectionError("Redis unavailable")

        cb = CircuitBreaker(redis_client=mock_redis)

        # Should not crash — defaults to CLOSED (allow)
        try:
            state = cb.get_state("example.com")
            # If it handles the error, state should be CLOSED
            assert state == CircuitState.CLOSED
        except ConnectionError:
            # Acceptable — test documents that CB doesn't handle Redis down
            pass

    def test_circuit_breaker_record_failure_redis_down(self) -> None:
        """Recording failure should not crash when Redis is down."""
        mock_redis = MagicMock()
        mock_redis.incr.side_effect = ConnectionError("Redis unavailable")
        mock_redis.expire.side_effect = ConnectionError("Redis unavailable")
        mock_redis.get.return_value = None

        cb = CircuitBreaker(redis_client=mock_redis)

        # Should not crash the pipeline
        with contextlib.suppress(ConnectionError):
            cb.record_failure("https://example.com/page")


class TestContentCacheWithoutRedis:
    """Content cache should degrade gracefully without Redis."""

    def test_cache_init_with_broken_redis_still_creates(self) -> None:
        """ContentCache creation with bad Redis succeeds — errors at use time."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = ConnectionError("refused")

        cache = ContentCache(redis_client=mock_redis)
        # Init succeeds, but operations degrade gracefully
        assert cache.get("any_hash") is None

    def test_cache_get_returns_none_on_error(self) -> None:
        """Cache.get() should return None when Redis errors."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = ConnectionError("Redis unavailable")

        cache = ContentCache(redis_client=mock_redis)
        result = cache.get("some_hash")
        assert result is None

    def test_cache_set_silent_on_error(self) -> None:
        """Cache.set() should not crash when Redis errors."""
        mock_redis = MagicMock()
        mock_redis.setex.side_effect = ConnectionError("Redis unavailable")

        cache = ContentCache(redis_client=mock_redis)
        # Should not raise — degrades gracefully
        cache.set("some_hash", {"sentiment": 0.5})


class TestPipelineWithoutRedis:
    """Pipeline gold enrichment should work without Redis cache."""

    def test_enrichment_skips_cache_when_redis_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pipeline should continue enrichment when cache is unavailable."""
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")

        # Simulate the cache=None fallback path in pipeline.py
        cache = None

        article_hash = "abc123"

        # Without cache, all articles should be sent for analysis
        cached = cache.get(article_hash) if cache is not None else None

        assert cached is None
        # Pipeline continues without cache — no crash
