"""Tests for circuit breaker (using fakeredis)."""

from unittest.mock import patch

import pytest

from mapear_rss.extraction.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    NoOpCircuitBreaker,
    build_circuit_breaker,
)


class FakeRedis:
    """Minimal in-memory Redis mock for testing."""

    def __init__(self) -> None:
        self._data: dict[str, str | bytes] = {}
        self._expiry: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        val = self._data.get(key)
        if val is None:
            return None
        if isinstance(val, str):
            return val.encode()
        return val

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, *keys: str) -> None:
        for k in keys:
            self._data.pop(k, None)

    def incr(self, key: str) -> int:
        val = int(self._data.get(key, 0))
        val += 1
        self._data[key] = str(val)
        return val

    def expire(self, key: str, seconds: int) -> None:
        self._expiry[key] = seconds

    def pipeline(self) -> "FakePipeline":
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list = []

    def set(self, key: str, value: str) -> "FakePipeline":
        self._ops.append(("set", key, value))
        return self

    def delete(self, *keys: str) -> "FakePipeline":
        self._ops.append(("delete", *keys))
        return self

    def execute(self) -> None:
        for op in self._ops:
            if op[0] == "set":
                self._redis.set(op[1], op[2])
            elif op[0] == "delete":
                self._redis.delete(*op[1:])


@pytest.fixture
def cb() -> CircuitBreaker:
    fake_redis = FakeRedis()
    breaker = CircuitBreaker(redis_client=fake_redis)  # type: ignore[arg-type]
    breaker.failure_threshold = 3
    breaker.recovery_timeout = 60
    return breaker


class TestCircuitBreaker:
    def test_starts_closed(self, cb: CircuitBreaker) -> None:
        state = cb.get_state("example.com")
        assert state == CircuitState.CLOSED

    def test_allows_when_closed(self, cb: CircuitBreaker) -> None:
        assert cb.is_allowed("https://example.com/page") is True

    def test_opens_after_threshold(self, cb: CircuitBreaker) -> None:
        url = "https://failing.com/page"
        for _ in range(3):
            cb.record_failure(url)

        assert cb.get_state("failing.com") == CircuitState.OPEN
        assert cb.is_allowed(url) is False

    def test_success_resets_failures(self, cb: CircuitBreaker) -> None:
        url = "https://example.com/page"
        cb.record_failure(url)
        cb.record_failure(url)
        cb.record_success(url)

        # Should not be open since success reset counter
        assert cb.get_state("example.com") == CircuitState.CLOSED

    def test_extract_domain(self) -> None:
        url1 = "https://g1.globo.com/rn/page"
        assert CircuitBreaker.extract_domain(url1) == "g1.globo.com"
        url2 = "https://tribunadonorte.com.br/feed/"
        assert CircuitBreaker.extract_domain(url2) == ("tribunadonorte.com.br")


class TestNoOpAndFactory:
    def test_noop_always_allows(self) -> None:
        cb = NoOpCircuitBreaker()
        assert cb.is_allowed("https://example.com/x") is True
        cb.record_failure("https://example.com/x")
        cb.record_success("https://example.com/x")
        assert cb.is_allowed("https://example.com/x") is True

    def test_build_returns_noop_when_disabled(self) -> None:
        from types import SimpleNamespace

        fake_settings = SimpleNamespace(redis=SimpleNamespace(enabled=False, url=""))
        with patch(
            "mapear_rss.extraction.circuit_breaker.get_rss_settings",
            return_value=fake_settings,
        ):
            cb = build_circuit_breaker()
        assert isinstance(cb, NoOpCircuitBreaker)
