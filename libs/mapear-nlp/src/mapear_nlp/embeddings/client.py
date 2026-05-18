"""Embedding client protocol and factory — Eixo 2 v2a.

Mirrors the LLM client shape (``mapear_nlp.llm.client``). One protocol,
one concrete implementation, lazy import so production images that
don't run the clustering job skip the heavy ``sentence-transformers`` /
``torch`` dep tree.

The narrative-clustering job is the single caller for v2a. v2b (stance)
and v2c (RAG) will reuse this same protocol — keeping it minimal
(``encode``, ``model``, ``dim``) so future consumers don't pay for
unused surface area.
"""

from __future__ import annotations

from typing import Protocol

from mapear_infra.config import EmbeddingsConfig


class EmbeddingError(RuntimeError):
    """Raised when an embedding call fails or returns no usable vector."""


class EmbeddingClient(Protocol):
    """Minimal text-to-vector interface used by the clustering job.

    A v1 consumer only needs ``encode`` (list-in, list-out, order-
    preserving) plus ``model`` and ``dim`` for lineage / schema. Richer
    interactions (batched API calls, async, GPU pooling) belong on a
    future class once a real bottleneck warrants them.
    """

    model: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Return one float vector per input text. Order-preserving."""
        ...


def get_embedding_client(cfg: EmbeddingsConfig) -> EmbeddingClient:
    """Return a concrete client for the configured embedding model.

    v2a only supports sentence-transformers (local inference, no API
    cost). Future providers — OpenAI text-embedding-3, Cohere
    embed-multilingual, Vertex text-embedding-gecko — slot in here.
    """
    from mapear_nlp.embeddings.sentence_transformer_client import (
        SentenceTransformerClient,
    )

    return SentenceTransformerClient(model=cfg.model)
