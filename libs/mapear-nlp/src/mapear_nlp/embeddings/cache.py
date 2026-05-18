"""Content-addressed GCS cache for narrative embedding vectors — Eixo 2 v2a.

Mirrors ``mapear_nlp.narrative_cache``. Cache key is
``<content_hash>_<embedding_model>.json``:

  - Re-running the clustering job on the same narrative emits the same
    key → cache hit, no encoder call.
  - Rotating the embedding model invalidates entries (new key) without
    deleting the old ones — analysts can compare two model vintages.

Best-effort: GCS errors fall back to live encoding. Failures are logged
but never raised — the embedding is a derived artifact, not a gate.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from google.cloud import storage


class EmbeddingCache:
    """GCS-backed content-addressed cache for embedding vectors."""

    def __init__(
        self,
        bucket: storage.Bucket,
        prefix: str,
    ) -> None:
        # Accept "narrative_embeddings" or "narrative_embeddings/" — strip
        # and re-attach the trailing slash so the prefix is canonical.
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"

    @classmethod
    def build(cls, *, bucket_name: str, project_id: str, prefix: str) -> EmbeddingCache:
        from google.cloud import storage

        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        return cls(bucket=bucket, prefix=prefix)

    @staticmethod
    def make_key(*, content_hash: str, embedding_model: str) -> str:
        # Slashes in model names (e.g. "intfloat/multilingual-e5-large")
        # would split the GCS path; replace.
        safe_model = embedding_model.replace("/", "_")
        return f"{content_hash}_{safe_model}.json"

    def _blob_path(self, key: str) -> str:
        return self._prefix + key

    def get(self, key: str) -> list[float] | None:
        """Return cached embedding vector or None on miss / error."""
        blob = self._bucket.blob(self._blob_path(key))
        try:
            if not blob.exists():
                return None
            raw = blob.download_as_text()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Embedding cache GET failed for {key}: {err}", key=key, err=exc
            )
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Embedding cache blob {key} is not valid JSON", key=key)
            return None
        vector = payload.get("embedding")
        if not isinstance(vector, list):
            logger.warning("Embedding cache blob {key} has no embedding list", key=key)
            return None
        return [float(x) for x in vector]

    def set(self, key: str, vector: list[float], *, embedding_model: str) -> None:
        """Persist vector; never raises."""
        blob = self._bucket.blob(self._blob_path(key))
        payload = {"embedding": vector, "embedding_model": embedding_model}
        try:
            blob.upload_from_string(
                json.dumps(payload, ensure_ascii=False),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Embedding cache SET failed for {key}: {err}", key=key, err=exc
            )
