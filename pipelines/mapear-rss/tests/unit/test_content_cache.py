"""Tests for content cache (using in-memory mock)."""

import pytest

from mapear_infra.cache import ContentCache


class FakeRedis:
    """Minimal in-memory Redis mock."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._data[key] = value.encode()

    def exists(self, key: str) -> int:
        return 1 if key in self._data else 0

    def delete(self, *keys: str) -> None:
        for k in keys:
            self._data.pop(k, None)


@pytest.fixture
def cache() -> ContentCache:
    return ContentCache(redis_client=FakeRedis(), ttl=3600)  # type: ignore[arg-type]


class TestContentCache:
    def test_get_returns_none_on_miss(self, cache: ContentCache) -> None:
        assert cache.get("nonexistent") is None

    def test_set_and_get(self, cache: ContentCache) -> None:
        data = {"sentiment_overall": 0.5, "topics": ["saúde"]}
        cache.set("hash123", data)
        result = cache.get("hash123")
        assert result == data

    def test_has(self, cache: ContentCache) -> None:
        assert cache.has("missing") is False
        cache.set("present", {"x": 1})
        assert cache.has("present") is True

    def test_invalidate(self, cache: ContentCache) -> None:
        cache.set("todelete", {"x": 1})
        assert cache.has("todelete") is True
        cache.invalidate("todelete")
        assert cache.has("todelete") is False

    def test_get_or_compute_caches(self, cache: ContentCache) -> None:
        call_count = 0

        def compute():
            nonlocal call_count
            call_count += 1
            return {"computed": True}

        result1 = cache.get_or_compute("h1", compute)
        result2 = cache.get_or_compute("h1", compute)

        assert result1 == {"computed": True}
        assert result2 == {"computed": True}
        assert call_count == 1  # Computed only once
