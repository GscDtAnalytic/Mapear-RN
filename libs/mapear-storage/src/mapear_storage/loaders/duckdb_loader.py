"""DuckDB implementation of WarehouseLoader for local development."""

import re
from pathlib import Path

import duckdb
from loguru import logger
from mapear_infra.config import get_settings

_VALID_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class DuckDBLoader:
    """Loads Parquet files into a local DuckDB database."""

    def __init__(self) -> None:
        db_path = get_settings().data_lake_path.parent / "mapear_rn.duckdb"
        self.conn = duckdb.connect(str(db_path))

    def _validate_table_name(self, name: str) -> str:
        """Validate table name against SQL injection."""
        if not _VALID_TABLE_NAME.match(name):
            raise ValueError(
                f"Invalid table name: {name!r}. "
                "Only alphanumeric characters and underscores allowed."
            )
        return name

    def load(
        self,
        source_path: str | Path,
        target_table: str,
        *,
        merge_key: str | None = None,
    ) -> int:
        table = self._validate_table_name(target_table)
        path = str(Path(source_path).resolve())

        # merge_key is a BigQuery-only upsert hint. DuckDB dev targets keep
        # the append behavior — dedup happens in staging views for local dbt.
        if merge_key is not None:
            logger.debug(
                "DuckDBLoader ignoring merge_key={key} for {table} "
                "(append-only in local dev)",
                key=merge_key,
                table=table,
            )

        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} AS "
            "SELECT * FROM read_parquet(?) WHERE 1=0",
            [path],
        )

        result = self.conn.execute(
            f"INSERT INTO {table} SELECT * FROM read_parquet(?)",
            [path],
        )

        row_count = result.fetchone()[0] if result else 0
        logger.info(
            "Loaded {rows} rows into {table}",
            rows=row_count,
            table=table,
        )
        return row_count
