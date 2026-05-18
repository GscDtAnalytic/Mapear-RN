"""Local filesystem implementation of StorageWriter."""

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger
from mapear_infra.config import get_settings


class LocalWriter:
    """Writes Parquet files to the local data lake directory."""

    def __init__(self) -> None:
        self.base_path = get_settings().data_lake_path

    def write_parquet(
        self,
        df: pd.DataFrame,
        layer: str,
        partition_key: str,
    ) -> str:
        target_dir = self.base_path / layer / partition_key
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / "data.parquet"
        df.to_parquet(file_path, engine="pyarrow", compression="snappy")

        logger.info(
            "Wrote {rows} rows to {path}",
            rows=len(df),
            path=str(file_path),
        )
        return str(file_path)

    def write_table(
        self,
        table: pa.Table,
        layer: str,
        partition_key: str,
    ) -> str:
        """Write a typed pa.Table preserving the explicit schema."""
        target_dir = self.base_path / layer / partition_key
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / "data.parquet"
        pq.write_table(table, file_path, compression="snappy")

        logger.info(
            "Wrote {rows} rows to {path}",
            rows=table.num_rows,
            path=str(file_path),
        )
        return str(file_path)
