"""Pub/Sub publisher for the RSS streaming path — Eixo 1 v2.

Publishes RawArticle records to the ``mapear-rss-raw`` topic so the
streaming consumer (Cloud Run Service) can process them inline with
NER + sentiment and write to Iceberg within ~1-2 minutes of scraping,
versus the 8-hour batch cycle.

Design choices:
- Fire-and-forget per article: a Pub/Sub failure must never block the
  batch pipeline.  The batch write path (BQ + Iceberg Stage 3.7) is
  the source of truth; Pub/Sub is an acceleration layer.
- content_hash as ordering_key: routes all messages for the same
  article to the same Pub/Sub partition, guaranteeing at-most-one
  delivery ordering per article when the consumer checks the key.
- JSON payload: keeps the consumer dependency-free from the domain
  model at the wire level.  The consumer deserialises into RawArticle
  via model_validate.

See docs/decisions/adr-eixo-1-v2-streaming-consumer.md.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from mapear_domain.models.base import RawArticle


def _serialise(article: RawArticle) -> bytes:
    """Serialise a RawArticle to UTF-8 JSON bytes for Pub/Sub."""
    data = article.model_dump(mode="json")
    return json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")


def _noop_callback(future: Future) -> None:  # type: ignore[type-arg]
    """Log publish errors without raising — fire-and-forget contract."""
    try:
        future.result()
    except Exception as exc:
        logger.warning("pubsub_publish_failed: {err}", err=exc)


class PubSubPublisher:
    """Publishes RawArticle messages to a Pub/Sub topic.

    Usage::

        publisher = PubSubPublisher.from_settings()
        publisher.publish(article)   # fire-and-forget

    The publisher is disabled (no-op) when ``MAPEAR_PUBSUB_ENABLED=false``
    or when the topic path is empty.  This keeps CI and local dev free of
    GCP credentials without conditional guards in call sites.
    """

    def __init__(self, topic_path: str, *, enabled: bool = True) -> None:
        self._topic_path = topic_path
        self._enabled = enabled and bool(topic_path)
        self._client = None  # lazy-init on first publish

    def _get_client(self):  # type: ignore[return]
        if self._client is None:
            from google.cloud import pubsub_v1

            self._client = pubsub_v1.PublisherClient()
        return self._client

    def publish(self, article: RawArticle) -> None:
        """Publish one RawArticle; never raises."""
        if not self._enabled:
            return
        try:
            client = self._get_client()
            data = _serialise(article)
            future = client.publish(
                self._topic_path,
                data=data,
                content_hash=article.content_hash,  # ordering key attribute
                source_type=article.source_type,
            )
            future.add_done_callback(_noop_callback)
        except Exception as exc:
            logger.warning(
                "pubsub_publish_error content_hash={ch}: {err}",
                ch=getattr(article, "content_hash", "?"),
                err=exc,
            )

    def publish_batch(self, articles: list[RawArticle]) -> int:
        """Publish a list of articles; returns count of publish calls initiated."""
        if not self._enabled or not articles:
            return 0
        for article in articles:
            self.publish(article)
        logger.info(
            "pubsub_batch_published: {n} articles → {topic}",
            n=len(articles),
            topic=self._topic_path,
        )
        return len(articles)

    @classmethod
    def from_settings(cls) -> PubSubPublisher:
        """Build from application settings."""
        from mapear_infra.config import get_settings

        settings = get_settings()
        cfg = settings.pubsub
        if not cfg.enabled:
            return cls("", enabled=False)
        project_id = settings.gcp.project_id
        if not project_id:
            logger.warning("GCP_PROJECT_ID not set — PubSub publisher disabled")
            return cls("", enabled=False)
        topic_path = f"projects/{project_id}/topics/{cfg.topic}"
        return cls(topic_path, enabled=True)
