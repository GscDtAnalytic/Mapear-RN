"""Per-table contract config for the codegen-driven schema pipeline.

One source of truth for the article triples (raw / silver / gold). The
Arrow schemas in `loaders.parquet_writer` and the BQ JSON generation
script (`scripts/generate_bq_schemas.py`) both read from here.

Each `TableContract` answers the questions the codegen needs:
  * which Pydantic model is the source of truth for this table?
  * is the layer permissive (every scalar nullable, raw-style)?
  * any field whose deployed nullability does not follow from the
    Pydantic Optional-ness?
  * is the deployed column order different from the Pydantic class?

Why these knobs exist
---------------------
permissive (raw): raw rows can land partially populated when an
  extractor crashes mid-flight. Every column is NULLABLE so partial
  inserts succeed. The Pydantic model still declares required fields —
  validation happens at ETL ingest, not at warehouse load.

nullable_overrides: V1 canonical computed fields (`content_rn_relevant`,
  `author_in_scope`) and a handful of late-added scalars (`source_type`
  in silver, `topic_label` in gold) must accept NULL for legacy rows
  even though Pydantic declares them as non-Optional.

field_order: silver_articles places the V1 computed fields between
  `resolution_confidence` and `actor_run_id` in the deployed schema.
  Pydantic's natural order (model_fields then model_computed_fields)
  puts them at the very end — so silver needs an explicit override.
"""

from __future__ import annotations

from dataclasses import dataclass

from mapear_domain.models.base import GoldArticle, RawArticle, SilverArticle
from mapear_domain.models.narrative import (
    SilverArticleStance,
    SilverNarrativeCluster,
    SilverNarrativeEmbedding,
)
from mapear_domain.models.shadow import SilverEventShadow
from pydantic import BaseModel

# V1 canonical computed bool fields — see schemas/__init__.py and the
# legacy comment in the original parquet_writer.py.
_V1_NULLABLE = frozenset({"content_rn_relevant", "author_in_scope"})


@dataclass(frozen=True)
class TableContract:
    """Codegen config for one warehouse table."""

    pydantic: type[BaseModel]
    permissive: bool = False
    nullable_overrides: frozenset[str] = frozenset()
    field_order: tuple[str, ...] | None = None


_SILVER_FIELD_ORDER: tuple[str, ...] = (
    "url",
    "source_feed",
    "title",
    "content_clean",
    "author",
    "published_at",
    "extracted_at",
    "content_hash",
    "entities",
    "mentioned_cities",
    "mentioned_mayors",
    "mentioned_governors",
    "mentioned_parties",
    "mentioned_persons",
    "is_rn_relevant",
    "source_type",
    "schema_version",
    "person_id",
    "scope_status",
    "resolution_confidence",
    "content_rn_relevant",
    "author_in_scope",
    "actor_run_id",
    "ingestion_run_id",
    "rule_version",
    "pipeline_version",
    # Stage 2B — tenant_id is the last lineage stamp on every row.
    "tenant_id",
)


ARTICLE_CONTRACTS: dict[str, TableContract] = {
    "raw_articles": TableContract(
        pydantic=RawArticle,
        permissive=True,
    ),
    "silver_articles": TableContract(
        pydantic=SilverArticle,
        nullable_overrides=_V1_NULLABLE | frozenset({"source_type"}),
        field_order=_SILVER_FIELD_ORDER,
    ),
    "gold_articles": TableContract(
        pydantic=GoldArticle,
        nullable_overrides=_V1_NULLABLE | frozenset({"topic_label"}),
    ),
    # Eixo 2 v2a — narrative embedding vectors, written by the out-of-band
    # clustering job. One row per (content_hash, embedding_model).
    "silver_narrative_embeddings": TableContract(
        pydantic=SilverNarrativeEmbedding,
    ),
    # Eixo 2 v2a — narrative cluster assignments, written by the same job.
    # One row per (cluster_run_date, region, algorithm, content_hash).
    "silver_narrative_clusters": TableContract(
        pydantic=SilverNarrativeCluster,
    ),
    # Eixo 2 v2b — stance labels, written by the out-of-band stance job.
    # One row per (content_hash, stance_prompt_version).
    "silver_article_stances": TableContract(
        pydantic=SilverArticleStance,
    ),
    # Stage 1E v2 — shadow A/B classifications, written inline by RSS and
    # social pipelines when MAPEAR_SHADOW_RULE_VERSION_YAML is set. One row
    # per (content_hash, shadow_rule_version).
    "silver_event_shadow": TableContract(
        pydantic=SilverEventShadow,
    ),
}


__all__ = ["ARTICLE_CONTRACTS", "TableContract"]
