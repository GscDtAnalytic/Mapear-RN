"""Tests for streaming consumer — Eixo 1 v2.

Covers:
- processor.ArticleProcessor: NER path, person resolution, Iceberg write
- main.py Flask app: /healthz, /push happy path, bad envelope, decode error,
  processing error
All external dependencies (NER, sentiment, resolver, Iceberg) are mocked
so tests run without GCP credentials or spaCy models.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make local scripts importable.
sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _raw_dict(**kwargs) -> dict:
    base = {
        "url": "https://example.com/art1",
        "source_feed": "https://example.com/feed",
        "title": "Governador visita Natal",
        "content": "O governador anunciou investimento em saúde.",
        "content_hash": "deadbeef01",
        "source_type": "rss",
        "extracted_at": "2026-05-13T10:00:00+00:00",
    }
    base.update(kwargs)
    return base


def _make_silver(content_hash: str = "deadbeef01"):
    """Return a minimal valid SilverArticle with all required fields populated."""
    from mapear_domain.models.base import SilverArticle

    return SilverArticle(
        url="https://example.com/art1",
        source_feed="https://example.com/feed",
        title="Governador visita Natal",
        content_clean="O governador anunciou investimento em saúde.",
        extracted_at=datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc),
        content_hash=content_hash,
        mentioned_persons=["Carlos Albuquerque"],
        source_type="rss",
    )


def _mock_processor_deps():
    """Return (ner_mock, sentiment_mock, resolver_mock, writer_mock)."""
    silver = _make_silver()

    ner = MagicMock()
    ner.extract_batch.return_value = [silver]

    resolution = MagicMock()
    resolution.person_id = "person-42"
    resolution.scope_status.value = "IN_SCOPE"
    resolution.confidence = 0.95

    resolver = MagicMock()
    resolver.resolve_best.return_value = resolution

    sentiment = MagicMock()
    sentiment.analyze_batch.return_value = [{"sentiment_overall": -0.3}]

    writer = MagicMock()

    return ner, sentiment, resolver, writer, silver


# ---------------------------------------------------------------------------
# ArticleProcessor tests
# ---------------------------------------------------------------------------


class TestArticleProcessor:
    def _build_processor(self):
        ner, sentiment, resolver, writer, silver = _mock_processor_deps()

        with (
            patch("processor._load_region", return_value=MagicMock()),
            patch("processor._build_ner", return_value=ner),
            patch("processor._build_sentiment", return_value=sentiment),
            patch("processor._build_resolver", return_value=resolver),
        ):
            from processor import ArticleProcessor

            proc = ArticleProcessor(region_id="rn", iceberg_writer=writer)

        # Expose internals for assertions
        proc._ner = ner
        proc._sentiment = sentiment
        proc._resolver = resolver
        proc._writer = writer
        proc._silver = silver
        return proc

    def test_process_returns_content_hash(self):
        proc = self._build_processor()
        result = proc.process(_raw_dict())
        assert result == "deadbeef01"

    def test_process_calls_ner(self):
        proc = self._build_processor()
        proc.process(_raw_dict())
        proc._ner.extract_batch.assert_called_once()

    def test_process_calls_resolver(self):
        proc = self._build_processor()
        proc.process(_raw_dict())
        proc._resolver.resolve_best.assert_called_once()

    def test_process_calls_sentiment(self):
        proc = self._build_processor()
        proc.process(_raw_dict())
        proc._sentiment.analyze_batch.assert_called_once()

    def test_process_stamps_source_type_rss_stream(self):
        proc = self._build_processor()
        proc.process(_raw_dict())
        assert proc._silver.source_type == "rss_stream"

    def test_process_stamps_person_id(self):
        proc = self._build_processor()
        proc.process(_raw_dict())
        assert proc._silver.person_id == "person-42"

    def test_process_stamps_scope_status(self):
        proc = self._build_processor()
        proc.process(_raw_dict())
        assert proc._silver.scope_status == "IN_SCOPE"

    def test_process_calls_iceberg_writer(self):
        proc = self._build_processor()
        with patch.object(proc, "_write_to_iceberg") as w:
            proc.process(_raw_dict())
            w.assert_called_once()

    def test_process_empty_ner_returns_hash_without_write(self):
        proc = self._build_processor()
        proc._ner.extract_batch.return_value = []
        with patch.object(proc, "_write_to_iceberg") as w:
            result = proc.process(_raw_dict())
        assert result == "deadbeef01"
        w.assert_not_called()

    def test_process_raises_on_ner_exception(self):
        proc = self._build_processor()
        proc._ner.extract_batch.side_effect = RuntimeError("model crash")
        with pytest.raises(RuntimeError, match="model crash"):
            proc.process(_raw_dict())


# ---------------------------------------------------------------------------
# Flask app tests
# ---------------------------------------------------------------------------


def _push_body(raw_dict: dict) -> dict:
    data_b64 = base64.b64encode(json.dumps(raw_dict).encode()).decode()
    return {"message": {"data": data_b64, "messageId": "msg-001"}}


class TestFlaskApp:
    @pytest.fixture(autouse=True)
    def _patch_processor(self):
        """Replace module-level _processor with a mock before importing app."""
        mock_proc = MagicMock()
        mock_proc.process.return_value = "deadbeef01"
        with patch.dict("sys.modules", {}):

            # Ensure main is re-imported with mocked processor
            if "main" in sys.modules:
                del sys.modules["main"]

            with (
                patch("processor.ArticleProcessor"),
                patch("processor._load_region"),
                patch("processor._build_ner"),
                patch("processor._build_sentiment"),
                patch("processor._build_resolver"),
                patch(
                    "mapear_storage.loaders.iceberg_writer.IcebergWriter.from_settings"
                ),
                patch("mapear_infra.logging.setup_logging"),
            ):
                import main as app_module  # noqa: PLC0415

                app_module._processor = mock_proc
                self._mock_proc = mock_proc
                self._app = app_module.app.test_client()
                yield

    def test_healthz_returns_200(self):
        resp = self._app.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_push_happy_path(self):
        resp = self._app.post(
            "/push",
            json=_push_body(_raw_dict()),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["content_hash"] == "deadbeef01"

    def test_push_calls_processor(self):
        self._app.post("/push", json=_push_body(_raw_dict()))
        self._mock_proc.process.assert_called_once()
        arg = self._mock_proc.process.call_args[0][0]
        assert arg["content_hash"] == "deadbeef01"

    def test_push_missing_message_key_returns_400(self):
        resp = self._app.post("/push", json={"bad": "envelope"})
        assert resp.status_code == 400

    def test_push_empty_body_returns_400(self):
        resp = self._app.post("/push", data="not json", content_type="text/plain")
        assert resp.status_code == 400

    def test_push_bad_base64_returns_400(self):
        resp = self._app.post(
            "/push",
            json={"message": {"data": "!!!not-base64!!!", "messageId": "x"}},
        )
        assert resp.status_code == 400

    def test_push_processor_error_returns_500(self):
        self._mock_proc.process.side_effect = RuntimeError("processing failed")
        resp = self._app.post("/push", json=_push_body(_raw_dict()))
        assert resp.status_code == 500
        assert "processing_error" in resp.get_json()["error"]

    def test_push_processor_error_triggers_pubsub_retry(self):
        """500 response means Pub/Sub will retry — verify we return 500, not 200."""
        self._mock_proc.process.side_effect = Exception("transient")
        resp = self._app.post("/push", json=_push_body(_raw_dict()))
        assert resp.status_code == 500
