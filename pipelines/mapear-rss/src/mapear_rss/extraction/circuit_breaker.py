"""Circuit breaker pattern for scraper resilience.

Tracks failures per domain in Redis. When a domain exceeds the failure
threshold, the circuit opens and requests to that domain are skipped
until the recovery timeout expires.

States:
  CLOSED  — normal operation, requests go through
  OPEN    — domain is failing, requests are blocked
  HALF_OPEN — recovery timeout expired, allow a few test requests
"""

import time
from enum import Enum
from typing import Protocol
from urllib.parse import urlparse

import redis
from loguru import logger

from mapear_rss.config import get_rss_settings


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerProtocol(Protocol):
    def is_allowed(self, url: str) -> bool: ...
    def record_success(self, url: str) -> None: ...
    def record_failure(self, url: str) -> None: ...


class NoOpCircuitBreaker:
    """No-op implementation used when Redis is disabled.

    Always allows requests — upstream resilience (domain cooldown,
    HTTP retries) remains in charge.
    """

    def is_allowed(self, url: str) -> bool:  # noqa: ARG002
        return True

    def record_success(self, url: str) -> None:  # noqa: ARG002
        return

    def record_failure(self, url: str) -> None:  # noqa: ARG002
        return


def build_circuit_breaker() -> CircuitBreakerProtocol:
    """Return a connected CircuitBreaker or NoOp when Redis is disabled."""
    settings = get_rss_settings()
    if not settings.redis.enabled:
        logger.info("Redis disabled via REDIS_ENABLED=false — circuit breaker is no-op")
        return NoOpCircuitBreaker()
    return CircuitBreaker()


class CircuitBreaker:
    """Per-domain circuit breaker backed by Redis."""

    KEY_PREFIX = "cb:"

    def __init__(self, redis_client: redis.Redis | None = None) -> None:
        settings = get_rss_settings()
        self.failure_threshold = settings.circuit_breaker.failure_threshold
        self.recovery_timeout = settings.circuit_breaker.recovery_timeout
        self.half_open_requests = settings.circuit_breaker.half_open_requests
        self._redis_unavailable_logged = False

        if redis_client is not None:
            self.redis = redis_client
        else:
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

    def _key(self, domain: str, field: str) -> str:
        return f"{self.KEY_PREFIX}{domain}:{field}"

    @staticmethod
    def extract_domain(url: str) -> str:
        """Extract the domain from a URL."""
        return urlparse(url).netloc

    def get_state(self, domain: str) -> CircuitState:
        """Return the current circuit state for a domain."""
        state = self.redis.get(self._key(domain, "state"))
        if state is None:
            return CircuitState.CLOSED
        return CircuitState(state.decode())

    def is_allowed(self, url: str) -> bool:
        """Check if a request to this URL's domain is allowed.

        Returns True if the circuit is closed or half-open with capacity.
        Fails open (allows request) if Redis is unavailable.
        """
        try:
            domain = self.extract_domain(url)
            state = self.get_state(domain)
        except redis.exceptions.ConnectionError:
            if not self._redis_unavailable_logged:
                logger.warning(
                    "Redis unavailable for circuit breaker — allowing all "
                    "requests (this warning will not repeat)"
                )
                self._redis_unavailable_logged = True
            return True

        if state == CircuitState.CLOSED:
            return True

        if state == CircuitState.OPEN:
            opened_at = self.redis.get(self._key(domain, "opened_at"))
            if opened_at and time.time() - float(opened_at) > self.recovery_timeout:
                self._transition(domain, CircuitState.HALF_OPEN)
                return True
            return False

        if state == CircuitState.HALF_OPEN:
            attempts = int(self.redis.get(self._key(domain, "half_open_count")) or 0)
            return attempts < self.half_open_requests

        return True

    def record_success(self, url: str) -> None:
        """Record a successful request — reset the circuit if half-open."""
        try:
            domain = self.extract_domain(url)
            state = self.get_state(domain)

            if state == CircuitState.HALF_OPEN:
                self._transition(domain, CircuitState.CLOSED)
                logger.info("Circuit CLOSED for {domain}", domain=domain)

            self.redis.delete(self._key(domain, "failures"))
        except redis.exceptions.ConnectionError:
            if not self._redis_unavailable_logged:
                logger.warning(
                    "Redis unavailable for circuit breaker — "
                    "state changes will not persist (this warning will not repeat)"
                )
                self._redis_unavailable_logged = True

    def record_failure(self, url: str) -> None:
        """Record a failed request — open the circuit if threshold exceeded."""
        try:
            domain = self.extract_domain(url)
            state = self.get_state(domain)

            if state == CircuitState.HALF_OPEN:
                self._transition(domain, CircuitState.OPEN)
                logger.warning(
                    "Circuit re-OPENED for {domain} (half-open test failed)",
                    domain=domain,
                )
                return

            failures = self.redis.incr(self._key(domain, "failures"))
            self.redis.expire(self._key(domain, "failures"), self.recovery_timeout * 2)

            if failures >= self.failure_threshold:
                self._transition(domain, CircuitState.OPEN)
                logger.warning(
                    "Circuit OPENED for {domain} after {failures} failures",
                    domain=domain,
                    failures=failures,
                )
        except redis.exceptions.ConnectionError:
            if not self._redis_unavailable_logged:
                logger.warning(
                    "Redis unavailable for circuit breaker — "
                    "state changes will not persist (this warning will not repeat)"
                )
                self._redis_unavailable_logged = True

    def _transition(self, domain: str, new_state: CircuitState) -> None:
        """Transition a domain to a new circuit state."""
        pipe = self.redis.pipeline()
        pipe.set(self._key(domain, "state"), new_state.value)

        if new_state == CircuitState.OPEN:
            pipe.set(self._key(domain, "opened_at"), str(time.time()))

        if new_state == CircuitState.HALF_OPEN:
            pipe.set(self._key(domain, "half_open_count"), "0")

        if new_state == CircuitState.CLOSED:
            pipe.delete(self._key(domain, "failures"))
            pipe.delete(self._key(domain, "opened_at"))
            pipe.delete(self._key(domain, "half_open_count"))

        pipe.execute()
