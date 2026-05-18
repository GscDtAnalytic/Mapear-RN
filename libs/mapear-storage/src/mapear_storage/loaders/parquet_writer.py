"""Typed parquet writer — eliminates schema drift between batches.

The earlier flow (`pd.DataFrame([m.model_dump(mode="json") for m in xs])`
+ `df.to_parquet(...)`) drops Pydantic type info and lets pyarrow infer
column types from Python values. Two failure modes:

- empty list columns become `list<int64>` while populated batches become
  `list<string>` — BigQuery refuses the union (`Parquet column ... has
  type BYTE_ARRAY which does not match the target cpp_type INT64`);
- `datetime` fields land as ISO strings and BQ writes the column as
  STRING, breaking time-based partitioning and forcing PARSE_TIMESTAMP
  in every downstream query.

This module pins one explicit `pyarrow.Schema` per warehouse table and
writes pa.Tables built against that schema, so the on-disk parquet
contract is stable regardless of payload contents.

Article triples (raw / silver / gold) are generated from Pydantic via
`pydantic_to_arrow`. Social and YouTube schemas are still hand-coded —
covered by Stage 1B and a later phase respectively.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pyarrow as pa
from pydantic import BaseModel

from mapear_storage.contracts import ARTICLE_CONTRACTS, TableContract
from mapear_storage.loaders.arrow_codegen import pydantic_to_arrow

# --- Reusable type building blocks ---------------------------------------

_TIMESTAMP = pa.timestamp("us", tz="UTC")

_ENTITY_STRUCT = pa.struct(
    [
        pa.field("text", pa.string()),
        pa.field("label", pa.string()),
    ]
)

_SENTIMENT_STRUCT = pa.struct(
    [
        pa.field("entity", pa.string()),
        pa.field("entity_type", pa.string()),
        pa.field("sentiment", pa.float64()),
        pa.field("mention_count", pa.int64()),
        # "entity" = score derived from sentences mentioning this entity;
        # "document" = no matching sentence found, fell back to overall score.
        pa.field("sentiment_source", pa.string()),
    ]
)

_DECISION_FACTOR_STRUCT = pa.struct(
    [
        pa.field("name", pa.string()),
        pa.field("value", pa.float64()),
        pa.field("weight", pa.float64()),
        pa.field("source", pa.string()),
    ]
)

_TRANSCRIPT_SEGMENT = pa.struct(
    [
        pa.field("start", pa.float64()),
        pa.field("duration", pa.float64()),
        pa.field("text", pa.string()),
    ]
)


# --- Per-table warehouse contracts ---------------------------------------
#
# Article triples are generated from Pydantic via codegen — Pydantic in
# `mapear-domain` is the single source of truth. The per-table knobs
# (permissive raw, nullable overrides, silver field order) live in
# `mapear_storage.contracts`. See ADR `data-contracts-pydantic-first`.


def arrow_from_contract(c: TableContract) -> pa.Schema:
    """Build an Arrow schema from a TableContract — public helper for callers
    that derive their own schemas (e.g. `mapear_social.parquet_schemas`)."""
    return pydantic_to_arrow(
        c.pydantic,
        permissive=c.permissive,
        nullable_overrides=c.nullable_overrides,
        field_order=list(c.field_order) if c.field_order else None,
    )


# Backwards-compat alias for in-module callers.
_arrow_from_contract = arrow_from_contract


RAW_ARTICLE_SCHEMA = _arrow_from_contract(ARTICLE_CONTRACTS["raw_articles"])
SILVER_ARTICLE_SCHEMA = _arrow_from_contract(ARTICLE_CONTRACTS["silver_articles"])
GOLD_ARTICLE_SCHEMA = _arrow_from_contract(ARTICLE_CONTRACTS["gold_articles"])
# Eixo 2 v2a — narrative clustering tables. Out-of-band job writes these.
NARRATIVE_EMBEDDING_SCHEMA = _arrow_from_contract(
    ARTICLE_CONTRACTS["silver_narrative_embeddings"]
)
NARRATIVE_CLUSTER_SCHEMA = _arrow_from_contract(
    ARTICLE_CONTRACTS["silver_narrative_clusters"]
)
# Eixo 2 v2b — stance labels. Out-of-band stance job writes this.
ARTICLE_STANCE_SCHEMA = _arrow_from_contract(
    ARTICLE_CONTRACTS["silver_article_stances"]
)
# Stage 1E v2 — shadow A/B classifications. RSS + social pipelines write
# this inline when MAPEAR_SHADOW_RULE_VERSION_YAML is set.
EVENT_SHADOW_SCHEMA = _arrow_from_contract(ARTICLE_CONTRACTS["silver_event_shadow"])

YT_RAW_VIDEO_SCHEMA = pa.schema(
    [
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("channel_id", pa.string(), nullable=False),
        pa.field("channel_name", pa.string(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=False),
        pa.field("published_at", _TIMESTAMP, nullable=False),
        pa.field("duration", pa.string()),
        pa.field("view_count", pa.int64(), nullable=False),
        pa.field("like_count", pa.int64(), nullable=False),
        pa.field("comment_count", pa.int64(), nullable=False),
        pa.field("tags", pa.list_(pa.string()), nullable=False),
        pa.field("default_language", pa.string()),
        pa.field("extracted_at", _TIMESTAMP, nullable=False),
        pa.field("source_type", pa.string(), nullable=False),
    ]
)

YT_RAW_COMMENT_SCHEMA = pa.schema(
    [
        pa.field("comment_id", pa.string(), nullable=False),
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("author_name", pa.string(), nullable=False),
        pa.field("text", pa.string(), nullable=False),
        pa.field("like_count", pa.int64(), nullable=False),
        pa.field("published_at", _TIMESTAMP, nullable=False),
        pa.field("parent_id", pa.string()),
        pa.field("extracted_at", _TIMESTAMP, nullable=False),
        pa.field("source_type", pa.string(), nullable=False),
    ]
)

YT_RAW_TRANSCRIPT_SCHEMA = pa.schema(
    [
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("language", pa.string(), nullable=False),
        pa.field("text", pa.string(), nullable=False),
        pa.field("segments", pa.list_(_TRANSCRIPT_SEGMENT), nullable=False),
        pa.field("is_auto_generated", pa.bool_(), nullable=False),
        pa.field("extracted_at", _TIMESTAMP, nullable=False),
        pa.field("source_type", pa.string(), nullable=False),
    ]
)

YT_SILVER_SCHEMA = pa.schema(
    [
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("channel_id", pa.string(), nullable=False),
        pa.field("channel_name", pa.string(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=False),
        pa.field("transcript_text", pa.string(), nullable=False),
        pa.field("transcript_status", pa.string(), nullable=False),
        pa.field("published_at", _TIMESTAMP, nullable=False),
        pa.field("duration", pa.string()),
        pa.field("view_count", pa.int64(), nullable=False),
        pa.field("like_count", pa.int64(), nullable=False),
        pa.field("comment_count", pa.int64(), nullable=False),
        pa.field("tags", pa.list_(pa.string()), nullable=False),
        pa.field("entities", pa.list_(_ENTITY_STRUCT), nullable=False),
        pa.field("mentioned_cities", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_persons", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_mayors", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_governors", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_parties", pa.list_(pa.string()), nullable=False),
        pa.field("is_rn_relevant", pa.bool_(), nullable=False),
        pa.field("sentiment_overall", pa.float64()),
        pa.field("sentiment_by_entity", pa.list_(_SENTIMENT_STRUCT), nullable=False),
        pa.field("source_type", pa.string(), nullable=False),
        pa.field("batch_id", pa.string(), nullable=False),
        pa.field("person_id", pa.string()),
        pa.field("scope_status", pa.string()),
        pa.field("resolution_confidence", pa.float64()),
        # V1 canonical fields — nullable because older pipeline versions
        # may not populate them.
        pa.field("content_rn_relevant", pa.bool_()),
        pa.field("author_in_scope", pa.bool_()),
    ]
)

# Social schemas (Facebook / Instagram / X / TikTok) live in
# `mapear_social.parquet_schemas` — keeps `mapear_storage` free of any
# `mapear_social` import (no more layer cycle).

YT_GOLD_SCHEMA = pa.schema(
    [
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("channel_id", pa.string(), nullable=False),
        pa.field("channel_name", pa.string(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("transcript_text", pa.string(), nullable=False),
        pa.field("transcript_status", pa.string(), nullable=False),
        pa.field("published_at", _TIMESTAMP, nullable=False),
        pa.field("view_count", pa.int64(), nullable=False),
        pa.field("like_count", pa.int64(), nullable=False),
        pa.field("comment_count", pa.int64(), nullable=False),
        pa.field("entities", pa.list_(_ENTITY_STRUCT), nullable=False),
        pa.field("mentioned_cities", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_persons", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_mayors", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_governors", pa.list_(pa.string()), nullable=False),
        pa.field("mentioned_parties", pa.list_(pa.string()), nullable=False),
        pa.field("is_rn_relevant", pa.bool_(), nullable=False),
        pa.field("sentiment_overall", pa.float64()),
        pa.field("sentiment_by_entity", pa.list_(_SENTIMENT_STRUCT), nullable=False),
        pa.field("comment_sentiment_avg", pa.float64()),
        pa.field("source_type", pa.string(), nullable=False),
        pa.field("batch_id", pa.string(), nullable=False),
    ]
)


_PRIMITIVES = (str, int, float, bool)


def _coerce_value(v: Any) -> Any:
    """Convert non-parquet-friendly Python values to friendly ones.

    Pydantic Url, UUID, Decimal, Enum, etc. -> str; datetime stays
    datetime (UTC-normalized); lists/dicts walked recursively.
    """
    if v is None or isinstance(v, _PRIMITIVES):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)
    if isinstance(v, list):
        return [_coerce_value(x) for x in v]
    if isinstance(v, tuple):
        return [_coerce_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _coerce_value(val) for k, val in v.items()}
    return str(v)


def _record_to_dict(record: BaseModel | dict) -> dict[str, Any]:
    data = record.model_dump() if isinstance(record, BaseModel) else dict(record)
    return {k: _coerce_value(v) for k, v in data.items()}


def records_to_dataframe(records: Iterable[BaseModel | dict]) -> pd.DataFrame:
    """Build a DataFrame from records preserving native Python types.

    Use this for inline quality validation before writing parquet.
    """
    return pd.DataFrame([_record_to_dict(r) for r in records])


def dataframe_to_table(df: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    """Cast a DataFrame to a pa.Table using the given schema.

    Adds missing schema columns as null, drops extras, reorders to schema.
    pyarrow then enforces the declared types — empty list cells become
    `list<string>` instead of pyarrow's inferred `list<int64>`.
    """
    df = df.copy()
    for field in schema:
        if field.name not in df.columns:
            df[field.name] = None
    df = df[[f.name for f in schema]]
    return pa.Table.from_pandas(df, schema=schema, preserve_index=False)


def write_dataframe_as_parquet(
    writer: Any,
    df: pd.DataFrame,
    schema: pa.Schema,
    layer: str,
    partition_key: str,
) -> str:
    """Write a DataFrame as parquet via the storage writer with a typed schema."""
    table = dataframe_to_table(df, schema)
    return writer.write_table(table, layer, partition_key)
