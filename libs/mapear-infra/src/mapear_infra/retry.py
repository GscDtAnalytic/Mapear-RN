"""Tenacity retry wrappers with exponential backoff."""

from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from loguru import logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

F = TypeVar("F", bound=Callable[..., Any])


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry attempt."""
    logger.warning(
        "Retrying {func} (attempt {attempt}/{max}): {error}",
        func=retry_state.fn.__name__ if retry_state.fn else "unknown",
        attempt=retry_state.attempt_number,
        max=retry_state.retry_object.stop.max_attempt_number,  # type: ignore[union-attr]
        error=str(retry_state.outcome.exception()) if retry_state.outcome else "",
    )


def _is_retryable_network_error(exc: BaseException) -> bool:
    """Check if an exception is a retryable network/HTTP error.

    Retries on: ConnectionError, TimeoutError, OSError,
    and HTTP 429 (rate limit) or 5xx (server errors).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, ConnectionError | TimeoutError | OSError)


def retry_on_network_error(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
) -> Callable[[F], F]:
    """Retry decorator for network operations (HTTP, DB, etc.)."""
    return retry(  # type: ignore[return-value]
        retry=retry_if_exception(_is_retryable_network_error),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=_log_retry,
        reraise=True,
    )


def retry_on_any_error(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
) -> Callable[[F], F]:
    """Retry decorator for any exception."""
    return retry(  # type: ignore[return-value]
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=_log_retry,
        reraise=True,
    )
