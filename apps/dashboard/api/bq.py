"""BigQuery client + cache layer for the FastAPI backend."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import pandas as pd
from cachetools import TTLCache
from google.cloud import bigquery

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project")
DATASET_GOLD = os.getenv("GCP_BQ_DATASET_GOLD", "mapear_gold")
DATASET_SILVER = os.getenv("GCP_BQ_DATASET_SILVER", "mapear_silver")
CACHE_TTL = int(os.getenv("CACHE_TTL_SEC", "900"))  # 15 min

_query_cache: TTLCache = TTLCache(maxsize=256, ttl=CACHE_TTL)


@lru_cache(maxsize=1)
def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID, location="southamerica-east1")


def tbl(name: str, dataset: str = DATASET_GOLD) -> str:
    return f"`{PROJECT_ID}.{dataset}.{name}`"


def _make_cache_key(sql: str, params: dict) -> tuple:
    def _hashable(v: object) -> object:
        return tuple(v) if isinstance(v, list) else v

    return (sql, tuple(sorted((k, _hashable(v)) for k, v in params.items())))


def query(sql: str, **params: object) -> pd.DataFrame:
    cache_key = _make_cache_key(sql, params)
    if cache_key in _query_cache:
        return _query_cache[cache_key]

    job_config = bigquery.QueryJobConfig(
        query_parameters=[_to_param(k, v) for k, v in params.items()]
    )
    df = _client().query(sql, job_config=job_config).to_dataframe()
    _query_cache[cache_key] = df
    return df


def _to_param(
    name: str, value: object
) -> bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter:
    from datetime import date, datetime

    if isinstance(value, bool):
        return bigquery.ScalarQueryParameter(name, "BOOL", value)
    if isinstance(value, int):
        return bigquery.ScalarQueryParameter(name, "INT64", value)
    if isinstance(value, float):
        return bigquery.ScalarQueryParameter(name, "FLOAT64", value)
    if isinstance(value, datetime):
        return bigquery.ScalarQueryParameter(name, "TIMESTAMP", value)
    if isinstance(value, date):
        return bigquery.ScalarQueryParameter(name, "DATE", value)
    if isinstance(value, list):
        elem_type = "STRING" if value and isinstance(value[0], str) else "INT64"
        return bigquery.ArrayQueryParameter(name, elem_type, value)
    return bigquery.ScalarQueryParameter(name, "STRING", str(value))


def df_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert DataFrame to JSON-safe list of dicts."""
    import math

    records = df.to_dict(orient="records")
    clean = []
    for row in records:
        cleaned = {}
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                cleaned[k] = v.isoformat()
            elif isinstance(v, float) and math.isnan(v):
                cleaned[k] = None
            else:
                cleaned[k] = v
        clean.append(cleaned)
    return clean
