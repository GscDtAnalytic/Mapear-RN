"""Tests for PubSubPublisher — Eixo 1 v2."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from mapear_domain.models.base import RawArticle


def _make_article(**kwargs) -> RawArticle:
    defaults = dict(
        url="https://example.com/article",
        source_feed="https://example.com/feed",
        title="Prefeito anuncia obras",
        content="Conteúdo do artigo sobre infraestrutura.",
        content_hash="abc123",
        source_type="rss",
    )
    defaults.update(kwargs)
    return RawArticle(**defaults)


class TestSerialise:
    def test_roundtrip(self):
        from mapear_storage.pubsub_publisher import _serialise

        article = _make_article()
        data = _serialise(article)
        recovered = json.loads(data.decode("utf-8"))
        assert recovered["content_hash"] == "abc123"
        assert recovered["title"] == "Prefeito anuncia obras"

    def test_url_serialised_as_string(self):
        from mapear_storage.pubsub_publisher import _serialise

        article = _make_article()
        data = _serialise(article)
        recovered = json.loads(data.decode("utf-8"))
        assert isinstance(recovered["url"], str)


class TestPubSubPublisher:
    def _publisher(self, enabled=True, topic="projects/p/topics/t"):
        from mapear_storage.pubsub_publisher import PubSubPublisher

        return PubSubPublisher(topic_path=topic, enabled=enabled)

    def test_disabled_noop(self):
        """Disabled publisher never initialises the GCP client."""
        pub = self._publisher(enabled=False)
        article = _make_article()
        with patch.object(pub, "_get_client") as mock_get:
            pub.publish(article)
            mock_get.assert_not_called()
        assert pub._client is None

    def test_empty_topic_noop(self):
        """Empty topic path disables the publisher."""
        pub = self._publisher(topic="")
        article = _make_article()
        with patch.object(pub, "_get_client") as mock_get:
            pub.publish(article)
            mock_get.assert_not_called()
        assert pub._client is None

    def test_publish_calls_client(self):
        pub = self._publisher()
        mock_future = MagicMock()
        mock_client = MagicMock()
        mock_client.publish.return_value = mock_future
        pub._client = mock_client

        pub.publish(_make_article())

        mock_client.publish.assert_called_once()
        call_kwargs = mock_client.publish.call_args
        assert call_kwargs.kwargs["content_hash"] == "abc123"

    def test_publish_never_raises_on_client_error(self):
        pub = self._publisher()
        mock_client = MagicMock()
        mock_client.publish.side_effect = RuntimeError("network error")
        pub._client = mock_client

        pub.publish(_make_article())  # must not raise

    def test_publish_batch_returns_count(self):
        pub = self._publisher()
        mock_future = MagicMock()
        mock_client = MagicMock()
        mock_client.publish.return_value = mock_future
        pub._client = mock_client

        articles = [_make_article(content_hash=f"h{i}") for i in range(3)]
        count = pub.publish_batch(articles)

        assert count == 3
        assert mock_client.publish.call_count == 3

    def test_publish_batch_empty_returns_zero(self):
        pub = self._publisher()
        assert pub.publish_batch([]) == 0

    def test_publish_batch_disabled_returns_zero(self):
        pub = self._publisher(enabled=False)
        articles = [_make_article()]
        assert pub.publish_batch(articles) == 0

    def test_from_settings_disabled_when_no_project(self, monkeypatch):
        """from_settings returns disabled publisher when GCP_PROJECT_ID unset."""
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        monkeypatch.setenv("MAPEAR_PUBSUB_ENABLED", "true")

        from mapear_storage.pubsub_publisher import PubSubPublisher

        pub = PubSubPublisher.from_settings()
        assert not pub._enabled

    def test_from_settings_disabled_by_flag(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "my-project")
        monkeypatch.setenv("MAPEAR_PUBSUB_ENABLED", "false")

        from mapear_storage.pubsub_publisher import PubSubPublisher

        pub = PubSubPublisher.from_settings()
        assert not pub._enabled

    def test_from_settings_enabled_builds_topic_path(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "my-project")
        monkeypatch.setenv("MAPEAR_PUBSUB_ENABLED", "true")
        monkeypatch.setenv("MAPEAR_PUBSUB_TOPIC", "mapear-rss-raw")

        from mapear_storage.pubsub_publisher import PubSubPublisher

        pub = PubSubPublisher.from_settings()
        assert pub._enabled
        assert pub._topic_path == "projects/my-project/topics/mapear-rss-raw"

    def test_callback_logs_future_error(self, caplog):
        """_noop_callback logs but does not raise on future failure."""
        from concurrent.futures import Future

        from mapear_storage.pubsub_publisher import _noop_callback

        f: Future = Future()  # type: ignore[type-arg]
        f.set_exception(RuntimeError("boom"))
        _noop_callback(f)  # must not raise
