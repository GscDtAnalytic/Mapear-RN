"""Cloud Run Service entrypoint — Eixo 1 v2 streaming consumer.

Receives Pub/Sub push notifications at POST /push and processes each
RawArticle inline (NER + sentiment + Iceberg write).

Pub/Sub push contract:
  POST /push
  Body: {"message": {"data": "<base64>", "messageId": "...", ...}}
  Returns 200 → Pub/Sub acks.
  Returns 4xx/5xx → Pub/Sub retries with exponential backoff.

The processor (ArticleProcessor) is a module-level singleton: NLP models
are loaded once at startup and reused across requests.  Cloud Run Service
with min-instances=1 keeps the models warm.

Health check:
  GET /healthz → 200 {"status": "ok"}
  Used by GCP Load Balancer and the Pub/Sub subscription health gate.

Environment variables:
  MAPEAR_REGION          (default: rn)
  GCP_PROJECT_ID         (required in prod)
  MAPEAR_ICEBERG_ENABLED (must be true)
  MAPEAR_ICEBERG_WAREHOUSE
  MAPEAR_ICEBERG_CATALOG_URI
  MAPEAR_ICEBERG_BIGLAKE_CONNECTION
  PORT                   (default: 8080, set by Cloud Run)
"""

from __future__ import annotations

import base64
import json
import os
import sys

from loguru import logger


def _setup_logging() -> None:
    from mapear_infra.logging import setup_logging

    setup_logging()


def _build_processor():  # type: ignore[return]
    """Initialise ArticleProcessor — called once at module load."""
    from mapear_storage.loaders.iceberg_writer import IcebergWriter

    from processor import ArticleProcessor

    region_id = os.environ.get("MAPEAR_REGION", "rn")
    writer = IcebergWriter.from_settings()
    return ArticleProcessor(region_id=region_id, iceberg_writer=writer)


_setup_logging()

logger.info("streaming_consumer: initialising NLP models and Iceberg writer…")
try:
    _processor = _build_processor()
    logger.info("streaming_consumer: ready")
except Exception as _init_err:
    logger.error("streaming_consumer: init failed: {err}", err=_init_err)
    sys.exit(1)


# --- Flask app ---

from flask import Flask, Response, jsonify, request  # noqa: E402

app = Flask(__name__)


@app.get("/healthz")
def healthz() -> Response:
    return jsonify({"status": "ok"})


@app.post("/push")
def push() -> tuple[Response, int]:
    """Handle one Pub/Sub push message."""
    envelope = request.get_json(silent=True)
    if not envelope or "message" not in envelope:
        logger.warning("push_bad_envelope: missing 'message' key")
        return jsonify({"error": "bad envelope"}), 400

    msg = envelope["message"]
    raw_data = msg.get("data", "")
    message_id = msg.get("messageId", "?")

    try:
        payload_bytes = base64.b64decode(raw_data)
        raw_dict = json.loads(payload_bytes)
    except Exception as exc:
        logger.warning(
            "push_decode_error messageId={mid}: {err}", mid=message_id, err=exc
        )
        # Return 400 → Pub/Sub will NOT retry malformed messages (correct).
        return jsonify({"error": "decode_error"}), 400

    try:
        content_hash = _processor.process(raw_dict)
        logger.debug(
            "push_acked messageId={mid} content_hash={ch}",
            mid=message_id,
            ch=content_hash,
        )
        return jsonify({"content_hash": content_hash}), 200
    except Exception as exc:
        logger.error(
            "push_processing_error messageId={mid}: {err}", mid=message_id, err=exc
        )
        # Return 500 → Pub/Sub retries (at-least-once; Iceberg is idempotent at
        # query time via dbt dedup on content_hash).
        return jsonify({"error": "processing_error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
