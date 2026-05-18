"""Content-based cache for enrichment results.

Stores enrichment outputs (sentiment, topics) keyed by content_hash
in Redis. Prevents reprocessing of syndicated content published
by multiple sources with identical content.
"""

import json

import redis
from loguru import logger

from mapear_infra.config import get_settings

DEFAULT_TTL = 60 * 60 * 24 * 7  # 7 days


class ContentCache:
    """Redis-backed cache keyed by content_hash."""

    KEY_PREFIX = "enrich:"

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        ttl: int = DEFAULT_TTL,
    ) -> None:
        if redis_client is not None:
            self.redis = redis_client
        else:
            settings = get_settings()
            connection_kwargs: dict = dict(
                retry_on_timeout=True,
                health_check_interval=30,
                socket_keepalive=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            if settings.redis.ssl:
                connection_kwargs["ssl_cert_reqs"] = None
            self.redis = redis.from_url(
                settings.redis.url,
                **connection_kwargs,
            )
        self.ttl = ttl
        self._unavailable_logged = False

    @classmethod
    def build(cls, ttl: int = DEFAULT_TTL) -> "ContentCache | None":
        """Factory: return a connected cache or None when REDIS_ENABLED=false.

        Use this in pipelines to avoid eager Redis connections when the
        deployment has Redis disabled (e.g., Cloud Run while the VPC
        connector is being diagnosed).
        """
        settings = get_settings()
        if not settings.redis.enabled:
            logger.info("Redis cache disabled via REDIS_ENABLED=false — skipping")
            return None
        try:
            return cls(ttl=ttl)
        except (ConnectionError, redis.RedisError) as exc:
            logger.warning(
                "Redis unavailable, enrichment cache disabled: {err}", err=exc
            )
            return None

    def _key(self, content_hash: str) -> str:
        return f"{self.KEY_PREFIX}{content_hash}"

    def _log_cache_error(
        self, operation: str, content_hash: str, error: Exception
    ) -> None:
        """Log cache errors, suppressing repeated warnings after the first."""
        if not self._unavailable_logged:
            logger.warning(
                "Redis cache unavailable ({op} failed for {hash}: {err}). "
                "Subsequent cache errors will be suppressed.",
                op=operation,
                hash=content_hash[:12],
                err=error,
            )
            self._unavailable_logged = True

    def get(self, content_hash: str) -> dict | None:
        """Return cached enrichment data, or None if not cached or on error."""
        try:
            raw = self.redis.get(self._key(content_hash))
        except (ConnectionError, redis.RedisError) as e:
            self._log_cache_error("get", content_hash, e)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set(self, content_hash: str, data: dict) -> None:
        """Cache enrichment results for a content_hash."""
        try:
            self.redis.setex(
                self._key(content_hash),
                self.ttl,
                json.dumps(data, ensure_ascii=False, default=str),
            )
        except (ConnectionError, redis.RedisError) as e:
            self._log_cache_error("set", content_hash, e)

    def has(self, content_hash: str) -> bool:
        """Check if enrichment data exists for this hash."""
        return bool(self.redis.exists(self._key(content_hash)))

    def invalidate(self, content_hash: str) -> None:
        """Remove cached data for a content_hash."""
        self.redis.delete(self._key(content_hash))

    def get_or_compute(
        self,
        content_hash: str,
        compute_fn: callable,
    ) -> dict:
        """Return cached result or compute and cache it."""
        cached = self.get(content_hash)
        if cached is not None:
            logger.debug(
                "Cache hit for {hash}",
                hash=content_hash[:12],
            )
            return cached

        result = compute_fn()
        self.set(content_hash, result)
        return result
