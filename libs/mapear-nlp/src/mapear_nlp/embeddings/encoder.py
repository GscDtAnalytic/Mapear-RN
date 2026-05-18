"""Cache-aware batch embedding encoder — Eixo 2 v2a.

Coordinates ``EmbeddingClient`` + ``EmbeddingCache`` over a list of
``(content_hash, text)`` pairs. Cache hits are returned without calling
the encoder; cache misses are batch-encoded in one shot and written
back to the cache.

The clustering job uses this once per run. Tests can pass an in-memory
cache and a stub client to assert hit/miss accounting without hitting
the network or the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from mapear_nlp.embeddings.cache import EmbeddingCache
from mapear_nlp.embeddings.client import EmbeddingClient, EmbeddingError


@dataclass(frozen=True)
class EncodeResult:
    """Outcome of one encoder call.

    ``vectors`` is in the same order as the input list. ``cache_hits``
    is the count of inputs satisfied by the cache; ``encoded`` is the
    count actually sent to the model. Used for audit + observability.
    """

    vectors: list[list[float]]
    cache_hits: int
    encoded: int


class CacheAwareEncoder:
    """Batch encode with content-addressed caching."""

    def __init__(
        self,
        client: EmbeddingClient,
        cache: EmbeddingCache | None,
    ) -> None:
        self._client = client
        self._cache = cache

    @property
    def model(self) -> str:
        return self._client.model

    @property
    def dim(self) -> int:
        return self._client.dim

    def encode_with_hashes(
        self,
        items: list[tuple[str, str]],
    ) -> EncodeResult:
        """Encode (content_hash, text) pairs. Order-preserving.

        Cache lookups happen first; misses are accumulated and sent to
        the encoder in one batch call. If the encoder raises, the
        whole batch fails — there is no per-item fallback because a
        partial result on a clustering job is worse than no result
        (would silently shift cluster boundaries).
        """
        if not items:
            return EncodeResult(vectors=[], cache_hits=0, encoded=0)

        vectors: list[list[float] | None] = [None] * len(items)
        miss_indices: list[int] = []
        miss_hashes: list[str] = []
        miss_texts: list[str] = []
        cache_hits = 0

        for i, (content_hash, text) in enumerate(items):
            if self._cache is not None:
                key = EmbeddingCache.make_key(
                    content_hash=content_hash, embedding_model=self.model
                )
                cached = self._cache.get(key)
                if cached is not None:
                    vectors[i] = cached
                    cache_hits += 1
                    continue
            miss_indices.append(i)
            miss_hashes.append(content_hash)
            miss_texts.append(text)

        if miss_texts:
            try:
                encoded = self._client.encode(miss_texts)
            except EmbeddingError:
                raise
            if len(encoded) != len(miss_texts):
                raise EmbeddingError(
                    f"encoder returned {len(encoded)} vectors for "
                    f"{len(miss_texts)} inputs"
                )
            for idx, ch, vec in zip(miss_indices, miss_hashes, encoded, strict=True):
                vectors[idx] = vec
                if self._cache is not None:
                    key = EmbeddingCache.make_key(
                        content_hash=ch, embedding_model=self.model
                    )
                    self._cache.set(key, vec, embedding_model=self.model)

        if any(v is None for v in vectors):
            # Defensive — every slot should be populated by either cache
            # or encode path. If this ever trips, log the gap before
            # falling over so the caller can debug.
            missing = [i for i, v in enumerate(vectors) if v is None]
            logger.error(
                "encoder left {n} slots empty: {idx}", n=len(missing), idx=missing
            )
            raise EmbeddingError(f"encoder left {len(missing)} slots empty")

        return EncodeResult(
            vectors=[v for v in vectors if v is not None],
            cache_hits=cache_hits,
            encoded=len(miss_texts),
        )
