"""BigQuery implementation of WarehouseLoader for production."""

import uuid
from pathlib import Path

from google.cloud import bigquery
from loguru import logger
from mapear_infra.config import get_settings


class BQLoader:
    """Loads Parquet files into BigQuery tables."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.gcp.project_id:
            raise ValueError(
                "GCP_PROJECT_ID is empty or not set. " "Check environment variables."
            )
        self.client = bigquery.Client(project=settings.gcp.project_id)

    def load(
        self,
        source_path: str | Path,
        target_table: str,
        *,
        merge_key: str | None = None,
    ) -> int:
        """Load a Parquet file into ``target_table``.

        When ``merge_key`` is provided, the load goes through a staging
        table and is merged into the target via BigQuery MERGE, so repeated
        runs do not duplicate rows by key. Without ``merge_key`` the load
        is a plain WRITE_APPEND (historical behavior).
        """
        parquet_options = bigquery.ParquetOptions()
        parquet_options.enable_list_inference = True

        uri = str(source_path)

        if merge_key is None:
            return self._load_append(uri, target_table, parquet_options)
        return self._load_merge(uri, target_table, merge_key, parquet_options)

    def _load_append(
        self,
        uri: str,
        target_table: str,
        parquet_options: "bigquery.ParquetOptions",
    ) -> int:
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            parquet_options=parquet_options,
        )

        if uri.startswith("gs://"):
            load_job = self.client.load_table_from_uri(
                uri, target_table, job_config=job_config
            )
        else:
            with open(uri, "rb") as f:
                load_job = self.client.load_table_from_file(
                    f, target_table, job_config=job_config
                )

        load_job.result()
        row_count = load_job.output_rows or 0

        logger.info(
            "Loaded {rows} rows into {table}",
            rows=row_count,
            table=target_table,
        )
        return row_count

    def _load_merge(
        self,
        uri: str,
        target_table: str,
        merge_key: str,
        parquet_options: "bigquery.ParquetOptions",
    ) -> int:
        staging_table = f"{target_table}__load_{uuid.uuid4().hex[:8]}"

        load_cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
            parquet_options=parquet_options,
        )

        if uri.startswith("gs://"):
            load_job = self.client.load_table_from_uri(
                uri, staging_table, job_config=load_cfg
            )
        else:
            with open(uri, "rb") as f:
                load_job = self.client.load_table_from_file(
                    f, staging_table, job_config=load_cfg
                )
        load_job.result()
        staging_rows = load_job.output_rows or 0

        try:
            table_ref = self.client.get_table(staging_table)
            cols = [f.name for f in table_ref.schema]
            if merge_key not in cols:
                raise ValueError(
                    f"merge_key={merge_key!r} not present in staging schema "
                    f"for {target_table}"
                )
            non_key_cols = [c for c in cols if c != merge_key]
            update_clause = ", ".join(f"T.`{c}` = S.`{c}`" for c in non_key_cols)
            insert_cols = ", ".join(f"`{c}`" for c in cols)
            insert_vals = ", ".join(f"S.`{c}`" for c in cols)

            # QUALIFY on staging handles within-batch duplicates so the MERGE
            # join key remains unique (MERGE errors otherwise).
            merge_sql = (
                f"MERGE `{target_table}` T "
                f"USING (SELECT * FROM `{staging_table}` "
                f"QUALIFY ROW_NUMBER() OVER "
                f"(PARTITION BY `{merge_key}` ORDER BY `{merge_key}`) = 1) S "
                f"ON T.`{merge_key}` = S.`{merge_key}` "
                f"WHEN MATCHED THEN UPDATE SET {update_clause} "
                f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
            )
            self.client.query(merge_sql).result()
        finally:
            self.client.delete_table(staging_table, not_found_ok=True)

        logger.info(
            "Merged {rows} rows into {table} on {key}",
            rows=staging_rows,
            table=target_table,
            key=merge_key,
        )
        return staging_rows

    def recent_ids(
        self,
        table_fqn: str,
        id_column: str,
        since_column: str,
        lookback_hours: int,
    ) -> set[str]:
        """Return distinct IDs ingested within the lookback window.

        Non-fatal on failure: logs a warning and returns an empty set so
        the caller degrades to no-dedup rather than aborting the run.
        """
        sql = (
            f"SELECT DISTINCT {id_column} FROM `{table_fqn}` "
            f"WHERE {since_column} > TIMESTAMP_SUB("
            "CURRENT_TIMESTAMP(), INTERVAL @lookback_hours HOUR)"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("lookback_hours", "INT64", lookback_hours)
            ]
        )
        try:
            rows = self.client.query(sql, job_config=job_config).result()
            return {row[0] for row in rows if row[0] is not None}
        except Exception as e:
            logger.warning(
                "recent_ids lookup failed for {table}: {err} "
                "— proceeding without dedup",
                table=table_fqn,
                err=str(e),
            )
            return set()
