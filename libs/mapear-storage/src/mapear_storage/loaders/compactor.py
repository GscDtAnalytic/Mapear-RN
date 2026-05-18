"""Parquet file compaction for the data lake.

Merges small Parquet files within each partition into a single
optimized file, improving query performance and reducing file count.
"""

from pathlib import Path

import pandas as pd
from loguru import logger
from mapear_infra.config import get_settings

MIN_FILE_SIZE_MB = 1
TARGET_FILE_SIZE_MB = 128


def compact_layer(layer: str, dry_run: bool = False) -> dict[str, int]:
    """Compact all partitions in a data lake layer."""
    settings = get_settings()
    layer_path = settings.data_lake_path / layer

    if not layer_path.exists():
        logger.info("Layer path {path} does not exist, skipping", path=layer_path)
        return {"partitions_compacted": 0, "files_merged": 0}

    stats = {"partitions_compacted": 0, "files_merged": 0, "partitions_failed": 0}

    for partition_dir in sorted(layer_path.iterdir()):
        if not partition_dir.is_dir():
            continue

        parquet_files = sorted(partition_dir.glob("**/*.parquet"))

        if len(parquet_files) <= 1:
            continue

        total_size = sum(f.stat().st_size for f in parquet_files)
        min_bytes = MIN_FILE_SIZE_MB * 1024 * 1024
        small_files = [f for f in parquet_files if f.stat().st_size < min_bytes]

        if not small_files and len(parquet_files) <= 2:
            continue

        logger.info(
            "Compacting {partition}: {files} files, {size_mb:.1f} MB",
            partition=partition_dir.name,
            files=len(parquet_files),
            size_mb=total_size / (1024 * 1024),
        )

        if dry_run:
            stats["partitions_compacted"] += 1
            stats["files_merged"] += len(parquet_files)
            continue

        try:
            _compact_partition(partition_dir, parquet_files)
            stats["partitions_compacted"] += 1
            stats["files_merged"] += len(parquet_files)
        except Exception as e:
            stats["partitions_failed"] += 1
            logger.error(
                "Failed to compact {partition}: {error}",
                partition=partition_dir.name,
                error=str(e),
            )

    logger.info(
        "Compaction complete for {layer}: "
        "{partitions} partitions, {files} files merged, {failed} failed",
        layer=layer,
        partitions=stats["partitions_compacted"],
        files=stats["files_merged"],
        failed=stats["partitions_failed"],
    )
    return stats


def _compact_partition(partition_dir: Path, parquet_files: list[Path]) -> None:
    """Merge multiple Parquet files into one within a partition."""
    dfs = [pd.read_parquet(f) for f in parquet_files]
    merged = pd.concat(dfs, ignore_index=True)

    if "content_hash" in merged.columns:
        before = len(merged)
        merged = merged.drop_duplicates(subset=["content_hash"], keep="last")
        dropped = before - len(merged)
        if dropped > 0:
            logger.info(
                "Dropped {count} duplicate rows during compaction",
                count=dropped,
            )

    compacted_path = partition_dir / "data_compacted.parquet"
    merged.to_parquet(
        compacted_path,
        engine="pyarrow",
        compression="snappy",
        index=False,
    )

    for f in parquet_files:
        f.unlink()

    final_path = partition_dir / "data.parquet"
    compacted_path.rename(final_path)

    logger.info(
        "Compacted {partition}: {rows} rows, {size_mb:.1f} MB",
        partition=partition_dir.name,
        rows=len(merged),
        size_mb=final_path.stat().st_size / (1024 * 1024),
    )


def compact_all_layers(dry_run: bool = False) -> dict[str, dict[str, int]]:
    """Run compaction on all data lake layers."""
    results = {}
    for layer in ("raw", "silver", "gold"):
        results[layer] = compact_layer(layer, dry_run=dry_run)
    return results
