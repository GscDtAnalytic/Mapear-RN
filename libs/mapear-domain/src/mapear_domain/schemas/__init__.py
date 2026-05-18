"""Typed sub-models that back the nested struct columns in the warehouse.

Before this module the struct payloads (`entities`, `sentiment_by_entity`,
`decision_factors`) were declared as `list[dict[str, str | float | int]]`
on the Pydantic models. The Arrow schemas in `mapear-storage/.../parquet_writer.py`
encoded the real shape; the Pydantic side dropped it.

Promoting these to Pydantic sub-models makes the domain models the single
source of truth: codegen reads them directly and produces matching Arrow
and BigQuery schemas (see `mapear_domain.schemas.bq_codegen` and
`mapear_storage.loaders.arrow_codegen`).

All fields are Optional to match the current nullable struct columns; the
producers (`SentimentAnalyzer`, `PoliticalSentimentClassifier`) populate
every field, but historic rows may have nulls.
"""

from pydantic import BaseModel


class EntityRef(BaseModel):
    """Named entity recognition output, one row of the `entities` list."""

    text: str | None = None
    label: str | None = None


class EntitySentiment(BaseModel):
    """Per-entity sentiment, one row of the `sentiment_by_entity` list.

    `sentiment_source` is "entity" when scored against sentences mentioning
    the entity, and "document" when no sentence matched and the document
    score was used as fallback.
    """

    entity: str | None = None
    entity_type: str | None = None
    sentiment: float | None = None
    mention_count: int | None = None
    sentiment_source: str | None = None


class DecisionFactor(BaseModel):
    """Storage representation of one signal in the political overlay.

    The compute-side dataclass lives in `mapear_nlp.political_sentiment`;
    its `as_dict()` output is what lands here when Gold rows are built.
    """

    name: str | None = None
    value: float | None = None
    weight: float | None = None
    source: str | None = None


__all__ = ["DecisionFactor", "EntityRef", "EntitySentiment"]
