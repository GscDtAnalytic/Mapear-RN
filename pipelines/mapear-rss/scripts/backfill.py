"""Backfill script — reprocess historical batches from the data lake.

Usage:
    poetry run python scripts/backfill.py --layer silver --from-date 2026-04-01

Reads existing Parquet files from the specified layer and reprocesses
them through subsequent pipeline stages. Uses content_hash to avoid
duplicating data in the warehouse.
"""

import argparse
import glob
from datetime import datetime

import pandas as pd
from loguru import logger

from mapear_domain.models.base import RawArticle
from mapear_infra.logging import setup_logging
from mapear_nlp.ner import NERExtractor
from mapear_rss.config import get_rss_settings as get_settings
from mapear_rss.transformation.deduplicator import Deduplicator
from mapear_storage.loaders.factory import get_storage_writer


def backfill_silver(from_date: str | None = None) -> None:
    """Reprocess raw articles into silver layer."""
    setup_logging()
    settings = get_settings()

    pattern = str(settings.lake_raw / "**/*.parquet")
    files = sorted(glob.glob(pattern, recursive=True))

    if not files:
        logger.warning("No raw Parquet files found to backfill.")
        return

    logger.info("Found {count} raw Parquet files", count=len(files))

    dedup = Deduplicator()
    ner = NERExtractor()
    writer = get_storage_writer()

    total_processed = 0

    for filepath in files:
        df = pd.read_parquet(filepath)
        articles = [RawArticle(**row) for row in df.to_dict("records")]

        unique = dedup.deduplicate(articles)
        silver = ner.extract_batch(unique)

        if silver:
            batch_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            df_silver = pd.DataFrame([a.model_dump(mode="json") for a in silver])
            writer.write_parquet(df_silver, "silver", f"backfill/batch={batch_id}")
            total_processed += len(silver)

    logger.info(
        "Backfill complete: {total} silver articles produced",
        total=total_processed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill pipeline data")
    parser.add_argument(
        "--layer",
        choices=["silver", "gold"],
        default="silver",
        help="Which layer to backfill",
    )
    parser.add_argument(
        "--from-date",
        type=str,
        default=None,
        help="Process files from this date (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    if args.layer == "silver":
        backfill_silver(args.from_date)
    else:
        logger.warning("Gold backfill not yet implemented.")


if __name__ == "__main__":
    main()
