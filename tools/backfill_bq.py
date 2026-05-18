"""Backfill BigQuery tables from existing GCS parquet files.

Usage:
    python scripts/backfill_bq.py [--dry-run] [--force] [--only TABLE]

Loads all parquet files from the GCS data lake into the corresponding
BigQuery tables. Designed to run once after enabling BQ loading in the
pipelines to backfill historical data.

Idempotency: a local checkpoint file (scripts/.backfill_bq_checkpoint.json)
records every URI that has been successfully loaded. Reruns skip URIs
present in the checkpoint so data is not duplicated. Use --force to
ignore the checkpoint and reload every file.

Error handling: failures are isolated per (table, batch). A failure in
one table does not abort the rest; the script exits with status 1 at
the end if any batch failed.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery, storage
from loguru import logger

TABLE_MAP = {
    "raw/batch=": "mapear_raw.raw_articles",
    "silver/batch=": "mapear_silver.silver_articles",
    "gold/batch=": "mapear_gold.gold_articles",
}

DEFAULT_BUCKET = "your-gcp-project-data-lake"
DEFAULT_PROJECT = "your-gcp-project"
CHECKPOINT_PATH = Path(__file__).parent / ".backfill_bq_checkpoint.json"

# Prefixes sorted by length (desc) to ensure most specific match wins.
_PREFIXES_BY_SPECIFICITY = sorted(TABLE_MAP.items(), key=lambda kv: -len(kv[0]))


def resolve_table(blob_name: str) -> str | None:
    for prefix, table in _PREFIXES_BY_SPECIFICITY:
        if blob_name.startswith(prefix):
            return table
    return None


def list_parquet_uris(bucket_name: str, project_id: str) -> list[tuple[str, str]]:
    """Return (gs_uri, bq_table) tuples for every parquet in the bucket."""
    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)

    results: list[tuple[str, str]] = []
    skipped = 0
    for blob in bucket.list_blobs():
        if not blob.name.endswith(".parquet"):
            continue
        table = resolve_table(blob.name)
        if table is None:
            skipped += 1
            logger.debug("Unmapped blob skipped: {name}", name=blob.name)
            continue
        results.append((f"gs://{bucket_name}/{blob.name}", table))

    if skipped:
        logger.warning("{count} parquet blobs did not match any prefix", count=skipped)

    results.sort(key=lambda x: x[0])
    return results


def load_checkpoint() -> set[str]:
    if not CHECKPOINT_PATH.exists():
        return set()
    try:
        data = json.loads(CHECKPOINT_PATH.read_text())
        return set(data.get("loaded_uris", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Checkpoint unreadable ({err}) — treating as empty", err=e)
        return set()


def save_checkpoint(loaded_uris: set[str]) -> None:
    payload = {"loaded_uris": sorted(loaded_uris)}
    CHECKPOINT_PATH.write_text(json.dumps(payload, indent=2))


def group_by_table(
    uris: list[tuple[str, str]], already_loaded: set[str]
) -> tuple[dict[str, list[str]], int]:
    by_table: dict[str, list[str]] = {}
    skipped = 0
    for uri, table in uris:
        if uri in already_loaded:
            skipped += 1
            continue
        by_table.setdefault(table, []).append(uri)
    return by_table, skipped


def backfill(
    bucket_name: str,
    project_id: str,
    dry_run: bool = False,
    force: bool = False,
    only_table: str | None = None,
) -> int:
    """Backfill all unloaded parquet files. Returns the number of failed batches."""
    start = time.monotonic()
    logger.info(
        "Scanning gs://{bucket} (project={project})",
        bucket=bucket_name,
        project=project_id,
    )
    uris = list_parquet_uris(bucket_name, project_id)
    if not uris:
        logger.warning("No parquet files found — nothing to backfill.")
        return 0

    checkpoint = set() if force else load_checkpoint()
    by_table, skipped_checkpoint = group_by_table(uris, checkpoint)

    if only_table:
        by_table = {t: us for t, us in by_table.items() if t == only_table}
        if not by_table:
            logger.warning("No pending files for --only {table}", table=only_table)

    logger.info(
        "Found {total} parquet files ({pending} pending, {done} already loaded)",
        total=len(uris),
        pending=sum(len(v) for v in by_table.values()),
        done=skipped_checkpoint,
    )
    for table, table_uris in sorted(by_table.items()):
        logger.info("  {table}: {count} pending", table=table, count=len(table_uris))

    if dry_run:
        logger.info("Dry run — no data loaded.")
        return 0

    if not by_table:
        logger.info("Nothing to load — exiting.")
        return 0

    bq_client = bigquery.Client(project=project_id)
    parquet_options = bigquery.ParquetOptions()
    parquet_options.enable_list_inference = True
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        autodetect=True,
        parquet_options=parquet_options,
    )

    totals: dict[str, int] = {}
    failures: dict[str, str] = {}
    loaded_uris = set(checkpoint)

    for table, table_uris in sorted(by_table.items()):
        logger.info(
            "Loading {count} files -> {table}", count=len(table_uris), table=table
        )
        t0 = time.monotonic()
        try:
            load_job = bq_client.load_table_from_uri(
                table_uris, table, job_config=job_config
            )
            load_job.result()
            rows = load_job.output_rows or 0
            totals[table] = rows
            loaded_uris.update(table_uris)
            logger.info(
                "OK {table}: {rows} rows in {secs:.1f}s",
                table=table,
                rows=rows,
                secs=time.monotonic() - t0,
            )
        except GoogleAPIError as e:
            failures[table] = str(e)
            logger.error("FAIL {table}: {err}", table=table, err=str(e))
        except Exception as e:
            failures[table] = f"unexpected: {e}"
            logger.exception("FAIL {table}: unexpected error", table=table)

        save_checkpoint(loaded_uris)

    elapsed = time.monotonic() - start
    total_rows = sum(totals.values())
    logger.info("=" * 60)
    logger.info("Backfill summary ({secs:.1f}s elapsed)", secs=elapsed)
    logger.info("  files scanned:   {n}", n=len(uris))
    logger.info("  files skipped:   {n} (checkpoint)", n=skipped_checkpoint)
    logger.info("  tables loaded:   {n}", n=len(totals))
    logger.info("  rows loaded:     {n}", n=total_rows)
    logger.info("  tables failed:   {n}", n=len(failures))
    for table, rows in sorted(totals.items()):
        logger.info("    {table}: {rows} rows", table=table, rows=rows)
    for table, err in sorted(failures.items()):
        logger.error("    {table}: {err}", table=table, err=err)

    return len(failures)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill BQ from GCS")
    parser.add_argument(
        "--dry-run", action="store_true", help="List files without loading"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore checkpoint and reload every file (use with caution)",
    )
    parser.add_argument(
        "--only", default=None, help="Limit backfill to a single BQ table"
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("GCP_GCS_BUCKET_NAME", DEFAULT_BUCKET),
        help="GCS bucket (defaults to $GCP_GCS_BUCKET_NAME or your-gcp-project-data-lake)",
    )
    parser.add_argument(
        "--project",
        default=os.getenv("GCP_PROJECT_ID", DEFAULT_PROJECT),
        help="GCP project (defaults to $GCP_PROJECT_ID or your-gcp-project)",
    )
    args = parser.parse_args()

    failures = backfill(
        bucket_name=args.bucket,
        project_id=args.project,
        dry_run=args.dry_run,
        force=args.force,
        only_table=args.only,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
