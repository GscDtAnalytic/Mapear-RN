"""Google Cloud Storage implementation of StorageWriter."""

import tempfile

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage
from loguru import logger
from mapear_infra.config import get_settings


class GCSWriter:
    """Writes Parquet files to a GCS bucket."""

    def __init__(self) -> None:
        settings = get_settings()
        self.bucket_name = settings.gcp.gcs_bucket_name
        if not self.bucket_name:
            raise ValueError(
                "GCP_GCS_BUCKET_NAME is empty or not set. "
                "Check environment variables (expected: GCP_GCS_BUCKET_NAME, "
                "not GCS_BUCKET_NAME)."
            )
        if not settings.gcp.project_id:
            raise ValueError(
                "GCP_PROJECT_ID is empty or not set. " "Check environment variables."
            )
        self.client = storage.Client(project=settings.gcp.project_id)
        self.bucket = self.client.bucket(self.bucket_name)

    def write_parquet(
        self,
        df: pd.DataFrame,
        layer: str,
        partition_key: str,
    ) -> str:
        blob_path = f"{layer}/{partition_key}/data.parquet"

        with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
            df.to_parquet(tmp.name, engine="pyarrow", compression="snappy")
            blob = self.bucket.blob(blob_path)
            blob.upload_from_filename(tmp.name)

        uri = f"gs://{self.bucket_name}/{blob_path}"
        logger.info("Wrote {rows} rows to {uri}", rows=len(df), uri=uri)
        return uri

    def write_table(
        self,
        table: pa.Table,
        layer: str,
        partition_key: str,
    ) -> str:
        """Write a typed pa.Table preserving the explicit schema."""
        blob_path = f"{layer}/{partition_key}/data.parquet"

        with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
            pq.write_table(table, tmp.name, compression="snappy")
            blob = self.bucket.blob(blob_path)
            blob.upload_from_filename(tmp.name)

        uri = f"gs://{self.bucket_name}/{blob_path}"
        logger.info("Wrote {rows} rows to {uri}", rows=table.num_rows, uri=uri)
        return uri
