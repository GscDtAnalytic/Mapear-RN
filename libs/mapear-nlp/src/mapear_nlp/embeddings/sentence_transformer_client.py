"""sentence-transformers concrete client — Eixo 2 v2a.

Wraps the local model. Heavy dep (~1.5GB once torch + the model
weights are on disk), so the import is guarded — pulling
``mapear_nlp.embeddings.client`` itself stays lazy-import free and
images that never run the clustering job pay nothing.
"""

from __future__ import annotations

from mapear_nlp.embeddings.client import EmbeddingError


class SentenceTransformerClient:
    """Local sentence-transformer encoder.

    The model is loaded eagerly at construction time (one disk read,
    one torch graph build) and held in memory for the lifetime of the
    instance. The clustering job constructs this once per run and
    reuses it across all narratives in the batch.

    ``dim`` is read off the model after load — different models have
    different output dims (mpnet=768, e5-large=1024, MiniLM-L6=384)
    and the embedding schema needs it stamped on every row for downstream
    audit.
    """

    def __init__(self, model: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover — import gate
            raise EmbeddingError(
                "sentence-transformers not installed. Install the optional "
                "'embeddings' group: poetry install --with embeddings"
            ) from exc

        self.model = model
        self._encoder = SentenceTransformer(model)
        self.dim = int(self._encoder.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            arr = self._encoder.encode(
                texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:  # noqa: BLE001 — surface as EmbeddingError
            raise EmbeddingError(f"encode failed: {exc}") from exc
        return [row.tolist() for row in arr]
