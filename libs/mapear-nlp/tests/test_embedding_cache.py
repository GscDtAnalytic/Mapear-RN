"""Unit tests for the embedding cache + cache-aware encoder — Eixo 2 v2a.

The encoder tests use an in-memory cache and a stub client so the
sentence-transformers / GCS dep tree is never imported.
"""

from __future__ import annotations

import pytest

from mapear_nlp.embeddings.cache import EmbeddingCache
from mapear_nlp.embeddings.client import EmbeddingError
from mapear_nlp.embeddings.encoder import CacheAwareEncoder


class _InMemoryCache:
    """In-memory stand-in for EmbeddingCache that preserves the API."""

    def __init__(self) -> None:
        self.store: dict[str, list[float]] = {}
        self.set_calls = 0
        self.get_calls = 0

    def get(self, key: str) -> list[float] | None:
        self.get_calls += 1
        return self.store.get(key)

    def set(self, key: str, vector: list[float], *, embedding_model: str) -> None:
        self.set_calls += 1
        self.store[key] = list(vector)


class _StubEmbeddingClient:
    """Encodes by hashing text length into a deterministic 4-dim vector."""

    def __init__(self, model: str = "stub-model", dim: int = 4) -> None:
        self.model = model
        self.dim = dim
        self.encode_calls = 0
        self.last_batch: list[str] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.encode_calls += 1
        self.last_batch = list(texts)
        return [[float(len(t)), 0.0, 0.0, 0.0] for t in texts]


# --- EmbeddingCache.make_key ------------------------------------------------


def test_make_key_combines_hash_and_model() -> None:
    key = EmbeddingCache.make_key(content_hash="abc", embedding_model="mpnet-base")
    assert key == "abc_mpnet-base.json"


def test_make_key_replaces_slashes_in_model_name() -> None:
    """Model names with slashes (intfloat/multilingual-e5-large) must be safe."""
    key = EmbeddingCache.make_key(
        content_hash="abc", embedding_model="intfloat/multilingual-e5-large"
    )
    assert "/" not in key
    assert key.startswith("abc_intfloat_multilingual-e5-large")


# --- CacheAwareEncoder ------------------------------------------------------


def test_encoder_empty_input_returns_empty_result() -> None:
    encoder = CacheAwareEncoder(client=_StubEmbeddingClient(), cache=_InMemoryCache())
    result = encoder.encode_with_hashes([])
    assert result.vectors == []
    assert result.cache_hits == 0
    assert result.encoded == 0


def test_encoder_calls_client_for_cache_misses() -> None:
    client = _StubEmbeddingClient()
    cache = _InMemoryCache()
    encoder = CacheAwareEncoder(client=client, cache=cache)
    result = encoder.encode_with_hashes([("h1", "hello"), ("h2", "hi")])
    assert client.encode_calls == 1
    assert client.last_batch == ["hello", "hi"]
    assert result.cache_hits == 0
    assert result.encoded == 2
    assert len(result.vectors) == 2
    # In-memory cache now has both entries.
    assert cache.set_calls == 2


def test_encoder_cache_hit_skips_client() -> None:
    client = _StubEmbeddingClient()
    cache = _InMemoryCache()
    cache.store[
        EmbeddingCache.make_key(content_hash="h1", embedding_model="stub-model")
    ] = [9.0, 9.0, 9.0, 9.0]
    encoder = CacheAwareEncoder(client=client, cache=cache)
    result = encoder.encode_with_hashes([("h1", "hello")])
    assert client.encode_calls == 0
    assert result.cache_hits == 1
    assert result.encoded == 0
    assert result.vectors == [[9.0, 9.0, 9.0, 9.0]]


def test_encoder_mixed_hits_and_misses_preserves_order() -> None:
    client = _StubEmbeddingClient()
    cache = _InMemoryCache()
    cache.store[
        EmbeddingCache.make_key(content_hash="h2", embedding_model="stub-model")
    ] = [42.0, 0.0, 0.0, 0.0]
    encoder = CacheAwareEncoder(client=client, cache=cache)
    result = encoder.encode_with_hashes(
        [("h1", "five5"), ("h2", "cached"), ("h3", "longer text")]
    )
    assert result.cache_hits == 1
    assert result.encoded == 2
    # Order preserved: h1 client (len=5), h2 cache (42), h3 client (len=11).
    assert result.vectors[0] == [5.0, 0.0, 0.0, 0.0]
    assert result.vectors[1] == [42.0, 0.0, 0.0, 0.0]
    assert result.vectors[2] == [11.0, 0.0, 0.0, 0.0]
    # Client only saw the misses, in order.
    assert client.last_batch == ["five5", "longer text"]


def test_encoder_no_cache_disables_lookup() -> None:
    client = _StubEmbeddingClient()
    encoder = CacheAwareEncoder(client=client, cache=None)
    result = encoder.encode_with_hashes([("h1", "hello")])
    assert client.encode_calls == 1
    assert result.cache_hits == 0
    assert result.encoded == 1


def test_encoder_raises_when_client_returns_wrong_count() -> None:
    class _BrokenClient:
        model = "broken"
        dim = 4

        def encode(self, texts: list[str]) -> list[list[float]]:
            # Returns one fewer vector than asked — corrupt response.
            return [[0.0, 0.0, 0.0, 0.0]] * (len(texts) - 1)

    encoder = CacheAwareEncoder(client=_BrokenClient(), cache=None)
    with pytest.raises(EmbeddingError, match="returned"):
        encoder.encode_with_hashes([("h1", "a"), ("h2", "b")])
