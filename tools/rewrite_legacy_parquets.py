"""Rewrite pre-fix parquet files in GCS using the typed schema from
mapear_storage.loaders.parquet_writer.

Context: the backfill (Fase 1b) failed for silver_articles, gold_articles
and gold_youtube_content because legacy parquets in GCS were produced by
the pre-fix pipeline with weak schemas (`published_at: string`,
`mentioned_*: list<null>`). This script reads each affected blob, coerces
datetime strings back to UTC datetimes, applies the helper's explicit
`pa.Schema` via `dataframe_to_table()` and uploads the rewritten parquet
in place, unblocking the BQ load.

Usage:
    cd Mapear-RSS && poetry run python ../scripts/rewrite_legacy_parquets.py \
        [--dry-run] [--prefix silver/batch=]
"""

import argparse
import io
import sys
from datetime import UTC, datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage
from loguru import logger

from mapear_storage.loaders.parquet_writer import (
    GOLD_ARTICLE_SCHEMA,
    RAW_ARTICLE_SCHEMA,
    SILVER_ARTICLE_SCHEMA,
    YT_GOLD_SCHEMA,
    YT_RAW_COMMENT_SCHEMA,
    YT_RAW_TRANSCRIPT_SCHEMA,
    YT_RAW_VIDEO_SCHEMA,
    YT_SILVER_SCHEMA,
    dataframe_to_table,
)

DEFAULT_BUCKET = "your-gcp-project-data-lake"
DEFAULT_PROJECT = "your-gcp-project"

# Prefixes must be matched most-specific first, same as backfill_bq.py.
PREFIX_TO_SCHEMA: dict[str, pa.Schema] = {
    "raw/youtube/videos/batch=": YT_RAW_VIDEO_SCHEMA,
    "raw/youtube/comments/batch=": YT_RAW_COMMENT_SCHEMA,
    "raw/youtube/transcripts/batch=": YT_RAW_TRANSCRIPT_SCHEMA,
    "silver/youtube/batch=": YT_SILVER_SCHEMA,
    "gold/youtube/batch=": YT_GOLD_SCHEMA,
    "raw/batch=": RAW_ARTICLE_SCHEMA,
    "silver/batch=": SILVER_ARTICLE_SCHEMA,
    "gold/batch=": GOLD_ARTICLE_SCHEMA,
}

_DATETIME_FIELDS = {
    name
    for schema in PREFIX_TO_SCHEMA.values()
    for name in [f.name for f in schema if pa.types.is_timestamp(f.type)]
}


def resolve_prefix(blob_name: str) -> str | None:
    matched = [p for p in PREFIX_TO_SCHEMA if blob_name.startswith(p)]
    if not matched:
        return None
    return max(matched, key=len)


def _parse_dt(v):
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.rstrip("Z")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return None


def normalize_dataframe(df: pd.DataFrame, schema: pa.Schema) -> pd.DataFrame:
    df = df.copy()
    for field in schema:
        if field.name in df.columns and pa.types.is_timestamp(field.type):
            df[field.name] = df[field.name].map(_parse_dt)
        if field.name in df.columns and pa.types.is_list(field.type):
            df[field.name] = df[field.name].map(
                lambda v: (
                    []
                    if v is None or (hasattr(v, "__len__") and len(v) == 0)
                    else list(v)
                )
            )
    return df


def rewrite_blob(blob, schema: pa.Schema, dry_run: bool) -> tuple[bool, int]:
    raw = blob.download_as_bytes()
    table = pq.read_table(io.BytesIO(raw))
    df = table.to_pandas()
    df = normalize_dataframe(df, schema)
    new_table = dataframe_to_table(df, schema)
    if dry_run:
        return True, new_table.num_rows
    buf = io.BytesIO()
    pq.write_table(new_table, buf)
    blob.upload_from_string(buf.getvalue(), content_type="application/octet-stream")
    return True, new_table.num_rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite legacy parquets to typed schema"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prefix",
        default=None,
        help=f"Restrict to one prefix (choices: {', '.join(PREFIX_TO_SCHEMA)})",
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args()

    if args.prefix and args.prefix not in PREFIX_TO_SCHEMA:
        logger.error("Unknown prefix {p}", p=args.prefix)
        return 2

    client = storage.Client(project=args.project)
    bucket = client.bucket(args.bucket)

    prefixes = [args.prefix] if args.prefix else list(PREFIX_TO_SCHEMA)
    total_ok = 0
    total_fail = 0
    total_rows = 0
    for prefix in prefixes:
        schema = PREFIX_TO_SCHEMA[prefix]
        logger.info("scanning prefix {p}", p=prefix)
        for blob in bucket.list_blobs(prefix=prefix):
            if not blob.name.endswith(".parquet"):
                continue
            try:
                ok, rows = rewrite_blob(blob, schema, args.dry_run)
                logger.info(
                    "{action} {name} ({rows} rows)",
                    action="DRY" if args.dry_run else "OK",
                    name=blob.name,
                    rows=rows,
                )
                total_ok += 1
                total_rows += rows
            except Exception as e:
                logger.exception("FAIL {name}: {err}", name=blob.name, err=e)
                total_fail += 1

    logger.info(
        "Done — ok={ok} fail={fail} rows={rows} dry={dry}",
        ok=total_ok,
        fail=total_fail,
        rows=total_rows,
        dry=args.dry_run,
    )
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
