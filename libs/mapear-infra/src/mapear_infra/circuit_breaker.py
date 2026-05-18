"""Thread-safe circuit breaker for platform adapters.

Transitions: CLOSED → OPEN (on failure_threshold failures) →
             HALF_OPEN (after recovery_timeout) → CLOSED (on success).

Usage::

    cb = CircuitBreaker("facebook", failure_threshold=3, recovery_timeout=120.0)
    if not cb.allow_call():
        raise CircuitBreakerOpen("facebook circuit is OPEN")
    try:
        result = do_api_call()
        cb.on_success()
    except Exception:
        cb.on_failure()
        raise
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):  # noqa: N818
    """Raised when a call is rejected because the circuit is OPEN."""


@dataclass
class CircuitBreaker:
    platform: str
    failure_threshold: int = 3
    recovery_timeout: float = 120.0
    half_open_max_calls: int = 1

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _failure_count: int = field(default=0, init=False, repr=False)
    _last_failure_time: float = field(default=0.0, init=False, repr=False)
    _half_open_calls: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow_call(self) -> bool:
        """Return True if the call should proceed."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info(
                        "Circuit breaker HALF_OPEN for {platform}"
                        " (after {elapsed:.0f}s)",
                        platform=self.platform,
                        elapsed=elapsed,
                    )
                    return True
                return False
            # HALF_OPEN
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

    def on_success(self) -> None:
        with self._lock:
            if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                logger.info(
                    "Circuit breaker CLOSED for {platform} (recovered)",
                    platform=self.platform,
                )
            self._state = CircuitState.CLOSED
            self._failure_count = 0

    def on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            should_open = (
                self._failure_count >= self.failure_threshold
                or self._state == CircuitState.HALF_OPEN
            )
            if should_open and self._state != CircuitState.OPEN:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker OPEN for {platform} "
                    "({failures} failures, recovery in {timeout:.0f}s)",
                    platform=self.platform,
                    failures=self._failure_count,
                    timeout=self.recovery_timeout,
                )
