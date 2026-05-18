"""Content-addressed GCS cache for LLM narrative summaries.

Eixo 2 v1. Volume is ALERT-only (~5% of pipeline rows), so GCS gives
us cheap, durable, content-addressed storage without the operational
overhead of a Redis instance. Cache key is
``<content_hash>_<rule_version>_<prompt_version>`` so:

  - Reprocessing the same article emits the same key → cache hit.
  - Changing the classifier rule_version invalidates entries (new key).
  - Swapping the prompt version invalidates entries (new key).

The cache is best-effort: if GCS is unreachable the pipeline falls
back to live LLM calls. Failures are logged but never raised — the
narrative summary is an explanatory layer, never a gate.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from google.cloud import storage


class NarrativeCache:
    """GCS-backed content-addressed cache for narrative summaries."""

    def __init__(
        self,
        bucket: storage.Bucket,
        prefix: str,
    ) -> None:
        # Strip and re-attach the trailing slash so callers can pass
        # "narrative_cache" or "narrative_cache/" interchangeably.
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"

    @classmethod
    def build(cls, *, bucket_name: str, project_id: str, prefix: str) -> NarrativeCache:
        """Construct a cache against a live GCS bucket."""
        from google.cloud import storage

        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        return cls(bucket=bucket, prefix=prefix)

    @staticmethod
    def make_key(
        *,
        content_hash: str,
        rule_version: str,
        prompt_version: str,
    ) -> str:
        # Slashes in versions would split the GCS path; replace.
        safe_rule = rule_version.replace("/", "_")
        safe_prompt = prompt_version.replace("/", "_")
        return f"{content_hash}_{safe_rule}_{safe_prompt}.json"

    def _blob_path(self, key: str) -> str:
        return self._prefix + key

    def get(self, key: str) -> dict | None:
        """Return cached payload or None on miss / error."""
        blob = self._bucket.blob(self._blob_path(key))
        try:
            if not blob.exists():
                return None
            raw = blob.download_as_text()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Narrative cache GET failed for {key}: {err}", key=key, err=exc
            )
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Narrative cache blob {key} is not valid JSON", key=key)
            return None

    def set(self, key: str, payload: dict) -> None:
        """Persist payload; never raises."""
        blob = self._bucket.blob(self._blob_path(key))
        try:
            blob.upload_from_string(
                json.dumps(payload, ensure_ascii=False),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Narrative cache SET failed for {key}: {err}", key=key, err=exc
            )
