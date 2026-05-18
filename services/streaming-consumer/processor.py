"""Streaming article processor — Eixo 1 v2.

Receives a decoded RawArticle dict, applies inline NER + sentiment +
person resolution, and writes the resulting SilverArticle to Iceberg.

Kept JSONL-pure at the boundary (dict in, nothing out) so it is
trivially testable without GCP credentials (same pattern as the graph
and NLP runner CLIs).

Processing scope (per-article, stateless):
  ✅ NERExtractor           — per-article, deterministic, safe for replay
  ✅ PersonResolver         — per-article, seed-file driven
  ✅ SentimentAnalyzer      — per-article, model-based
  ❌ PoliticalSentimentClassifier — needs batch-level volume/velocity
  ❌ TopicModeler           — TF-IDF across the full batch corpus
  ❌ NarrativeExplainer     — LLM cost per message is unacceptable

SilverArticles written here will have ``sentiment_label=None``.
The batch pipeline continues to classify; dbt marts COALESCE both paths.

source_type is stamped as "rss_stream" to distinguish streaming-produced
rows from batch-produced rows in the Iceberg table. The dbt
stg_articles_unified view deduplicates by content_hash.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from mapear_domain.models.base import SilverArticle
    from mapear_storage.loaders.iceberg_writer import IcebergWriter

_PIPELINE_VERSION = "stream-v1"
_SOURCE_TYPE = "rss_stream"


def _load_region(region_id: str):  # type: ignore[return]
    from mapear_domain.region import load_region

    return load_region(region_id)


def _build_ner(region) -> Any:  # noqa: ANN401
    from mapear_nlp.ner import NERExtractor

    return NERExtractor(region=region)


def _build_sentiment() -> Any:  # noqa: ANN401
    from mapear_nlp.sentiment import SentimentAnalyzer

    return SentimentAnalyzer()


def _build_resolver(region: Any) -> Any:  # noqa: ANN401
    from mapear_domain.entity_resolution import PersonResolver, set_targets_seed_path

    for candidate in (
        Path("dbt/seeds/rn_targets.csv"),
        Path("../dbt/seeds/rn_targets.csv"),
        Path("/app/dbt/seeds/rn_targets.csv"),
    ):
        if candidate.exists():
            set_targets_seed_path(candidate)
            break

    return PersonResolver(region=region)


class ArticleProcessor:
    """Stateful processor — NLP models loaded once, reused across messages.

    Designed to be instantiated once per Cloud Run Service instance (module-
    level singleton) so model load time is amortized across many messages.
    """

    def __init__(
        self,
        region_id: str,
        iceberg_writer: IcebergWriter,
        *,
        rn_feed_urls: frozenset[str] | None = None,
    ) -> None:
        self._region = _load_region(region_id)
        self._ner = _build_ner(self._region)
        self._sentiment = _build_sentiment()
        self._resolver = _build_resolver(self._region)
        self._writer = iceberg_writer
        self._rn_feed_urls: frozenset[str] = rn_feed_urls or frozenset()

    def process(self, raw_dict: dict) -> str:
        """Process one RawArticle dict; returns content_hash on success.

        Raises on unrecoverable errors so the caller can return a non-200
        to Pub/Sub, triggering at-least-once retry.
        """
        from mapear_domain.models.base import RawArticle

        raw = RawArticle.model_validate(raw_dict)
        ingestion_run_id = f"rss-stream-{uuid.uuid4().hex[:12]}"

        # NER → SilverArticle (single-article path; extract_batch([raw]))
        silvers = self._ner.extract_batch([raw], rn_feed_urls=self._rn_feed_urls)
        if not silvers:
            logger.warning("stream_ner_empty content_hash={ch}", ch=raw.content_hash)
            return raw.content_hash

        silver = silvers[0]

        # Person resolution
        res = self._resolver.resolve_best(
            mentions=silver.mentioned_persons,
            context=f"{silver.title}. {silver.content_clean}",
            platform="rss",
        )
        silver.person_id = res.person_id
        silver.scope_status = res.scope_status.value
        silver.resolution_confidence = res.confidence
        silver.ingestion_run_id = ingestion_run_id
        silver.pipeline_version = _PIPELINE_VERSION
        silver.source_type = _SOURCE_TYPE

        # Sentiment (single-article)
        sentiments = self._sentiment.analyze_batch([silver])
        sent = sentiments[0] if sentiments else {}

        # Write to Iceberg — appends to same silver_articles table as batch.
        # Deduplication by content_hash is handled by the dbt mart.
        self._write_to_iceberg(silver, sent)

        logger.info(
            "stream_processed content_hash={ch} person_id={pid} "
            "sentiment={s:.3f} run={run}",
            ch=silver.content_hash,
            pid=silver.person_id or "none",
            s=sent.get("sentiment_overall", 0.0),
            run=ingestion_run_id,
        )
        return silver.content_hash

    def _write_to_iceberg(self, silver: SilverArticle, sent: dict) -> None:
        from mapear_storage.loaders.parquet_writer import (
            SILVER_ARTICLE_SCHEMA,
            dataframe_to_table,
            records_to_dataframe,
        )

        df = records_to_dataframe([silver])
        # dataframe_to_table enforces the Iceberg schema: adds missing columns
        # as null and drops extras (e.g. sentiment_overall, sentiment_by_entity)
        # that are in SilverArticle but not in the stored Iceberg table schema.
        table = dataframe_to_table(df, SILVER_ARTICLE_SCHEMA)
        self._writer.append(table, "silver_articles")
        logger.debug("stream_iceberg_write content_hash={ch}", ch=silver.content_hash)
