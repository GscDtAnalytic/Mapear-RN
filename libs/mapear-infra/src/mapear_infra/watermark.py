from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from mapear_infra.config import get_settings

WATERMARK_GCS_BLOB = "metadata/watermarks.json"
VALID_KEYS = frozenset({"rss", "facebook", "instagram", "tiktok", "x"})


class WatermarkManager:
    """Reads and writes per-pipeline temporal watermarks.

    Environment-aware:
    - Production: GCS blob ``metadata/watermarks.json`` in the data-lake bucket.
    - Local dev:  ``{settings.data_lake_path}/metadata/watermarks.json``.

    JSON format — flat dict of pipeline_key → ISO-8601 UTC timestamp:
        { "rss": "2026-04-23T10:00:00+00:00", "facebook": "..." }

    Usage::

        wm = WatermarkManager("rss")
        cutoff = wm.get_watermark()        # None on first run
        # ... pipeline work ...
        wm.save_watermark(run_started_at)  # call only on success
    """

    def __init__(self, pipeline_key: str) -> None:
        if pipeline_key not in VALID_KEYS:
            raise ValueError(
                f"Unknown pipeline_key {pipeline_key!r}. "
                f"Must be one of: {sorted(VALID_KEYS)}"
            )
        self.pipeline_key = pipeline_key
        self._settings = get_settings()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_watermark(self) -> datetime | None:
        """Return the last-successful-run timestamp, or None on first run.

        Raises on GCS connectivity failure so the pipeline fails loudly
        rather than silently re-ingesting all history.
        """
        data = self._read_all()
        raw = data.get(self.pipeline_key)
        if not raw:
            logger.info(
                "No watermark found for {key} — treating as first run",
                key=self.pipeline_key,
            )
            return None
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            logger.info(
                "Watermark loaded for {key}: {ts}",
                key=self.pipeline_key,
                ts=dt.isoformat(),
            )
            return dt
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Watermark for {key} is not a valid ISO timestamp ({raw!r}): {err}. "
                "Treating as first run.",
                key=self.pipeline_key,
                raw=raw,
                err=exc,
            )
            return None

    def save_watermark(self, timestamp: datetime) -> None:
        """Persist timestamp as the new watermark for this pipeline_key.

        Logs error but does NOT raise on write failure — the pipeline already
        succeeded and the next run will catch up with a small overlap.
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        try:
            data = self._read_all()
        except Exception as exc:
            logger.error(
                "Could not read existing watermarks before save ({err}); "
                "aborting save to avoid clobbering other pipeline keys.",
                err=exc,
            )
            return
        data[self.pipeline_key] = timestamp.isoformat()
        try:
            self._write_all(data)
            logger.info(
                "Watermark saved for {key}: {ts}",
                key=self.pipeline_key,
                ts=timestamp.isoformat(),
            )
        except Exception as exc:
            logger.error(
                "Failed to save watermark for {key}: {err}. "
                "Next run will re-ingest since {ts}.",
                key=self.pipeline_key,
                err=exc,
                ts=timestamp.isoformat(),
            )

    # ------------------------------------------------------------------
    # Private: environment-aware dispatch
    # ------------------------------------------------------------------

    def _read_all(self) -> dict[str, str]:
        if self._settings.is_local:
            return self._read_local()
        return self._read_gcs()

    def _write_all(self, data: dict[str, str]) -> None:
        if self._settings.is_local:
            self._write_local(data)
        else:
            self._write_gcs(data)

    # --- GCS backend ---

    def _gcs_client(self):
        from google.cloud import storage  # lazy import — not available in local dev

        return storage.Client(project=self._settings.gcp.project_id)

    def _read_gcs(self) -> dict[str, str]:
        client = self._gcs_client()
        bucket = client.bucket(self._settings.gcp.gcs_bucket_name)
        blob = bucket.blob(WATERMARK_GCS_BLOB)
        if not blob.exists():
            return {}
        raw = blob.download_as_text(encoding="utf-8")
        return json.loads(raw)

    def _write_gcs(self, data: dict[str, str]) -> None:
        client = self._gcs_client()
        bucket = client.bucket(self._settings.gcp.gcs_bucket_name)
        blob = bucket.blob(WATERMARK_GCS_BLOB)
        blob.upload_from_string(
            json.dumps(data, ensure_ascii=False, indent=2),
            content_type="application/json",
        )

    # --- Local backend ---

    def _local_path(self) -> Path:
        return self._settings.data_lake_path / "metadata" / "watermarks.json"

    def _read_local(self) -> dict[str, str]:
        p = self._local_path()
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def _write_local(self, data: dict[str, str]) -> None:
        p = self._local_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
