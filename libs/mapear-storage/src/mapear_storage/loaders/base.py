"""Abstract interfaces for storage and warehouse backends.

Uses Protocol classes so implementations don't need to inherit — just
implement the same method signatures.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd
import pyarrow as pa


@runtime_checkable
class StorageWriter(Protocol):
    """Writes DataFrames to the data lake (local filesystem or GCS)."""

    def write_parquet(
        self,
        df: pd.DataFrame,
        layer: str,
        partition_key: str,
    ) -> str:
        """Write a DataFrame as Parquet to the given layer."""
        ...

    def write_table(
        self,
        table: pa.Table,
        layer: str,
        partition_key: str,
    ) -> str:
        """Write a typed pa.Table as Parquet, preserving the explicit schema."""
        ...


@runtime_checkable
class WarehouseLoader(Protocol):
    """Loads data into the analytical warehouse (DuckDB or BigQuery)."""

    def load(
        self,
        source_path: str | Path,
        target_table: str,
        *,
        merge_key: str | None = None,
    ) -> int:
        """Load a Parquet file into the warehouse.

        When ``merge_key`` is provided, implementations that support MERGE
        semantics should upsert by that key to prevent duplicates across
        overlapping runs. Implementations that do not support MERGE (e.g.
        DuckDB local dev) may ignore the hint and fall back to append.
        """
        ...
