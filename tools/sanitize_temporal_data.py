"""Sanitation script for temporal data governance.

Identifies social posts in BigQuery that predate the electoral cutoff (2025-01-01)
or have a missing data_type, and optionally marks them as 'backfill' so they are
excluded from the default analytical view (stg_social__silver_posts).

Usage:
    # Dry run — report only (default)
    python scripts/sanitize_temporal_data.py

    # Dry run with a different cutoff
    python scripts/sanitize_temporal_data.py --cutoff-date=2025-03-01

    # Apply the fix (UPDATE data_type = 'backfill')
    python scripts/sanitize_temporal_data.py --apply

    # Apply to a specific project/dataset
    python scripts/sanitize_temporal_data.py --apply \\
        --project=mapear-prod --dataset=mapear_silver

Exit codes:
    0  Clean — no pre-cutoff or untagged rows found (or --apply succeeded)
    1  Pre-cutoff rows found (dry run); fix by re-running with --apply
    2  BQ error
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

ELECTORAL_CUTOFF_DATE = date(2025, 1, 1)
SILVER_TABLE = "silver_social_posts"
PLATFORMS = ("facebook", "instagram", "x", "tiktok")


# ---------------------------------------------------------------------------
# BQ helpers
# ---------------------------------------------------------------------------


def _bq_client(project: str | None):
    try:
        from google.cloud import bigquery  # type: ignore[import-untyped]
    except ImportError:
        print(
            "ERROR: google-cloud-bigquery is not installed. "
            "Run: pip install google-cloud-bigquery",
            file=sys.stderr,
        )
        sys.exit(2)
    return bigquery.Client(project=project)


def _audit_query(dataset: str, cutoff_iso: str) -> str:
    return f"""
SELECT
    platform,
    data_type,
    COUNT(*) AS row_count,
    MIN(published_at) AS min_published_at,
    MAX(published_at) AS max_published_at
FROM `{dataset}.{SILVER_TABLE}`
WHERE published_at < TIMESTAMP('{cutoff_iso}')
   OR data_type IS NULL
   OR data_type NOT IN ('incremental', 'backfill')
GROUP BY platform, data_type
ORDER BY platform, data_type
"""


def _update_query(dataset: str, cutoff_iso: str) -> str:
    return f"""
UPDATE `{dataset}.{SILVER_TABLE}`
SET data_type = 'backfill'
WHERE published_at < TIMESTAMP('{cutoff_iso}')
   OR data_type IS NULL
   OR data_type NOT IN ('incremental', 'backfill')
"""


def _count_query(dataset: str, cutoff_iso: str) -> str:
    return f"""
SELECT COUNT(*) AS total
FROM `{dataset}.{SILVER_TABLE}`
WHERE published_at < TIMESTAMP('{cutoff_iso}')
   OR data_type IS NULL
   OR data_type NOT IN ('incremental', 'backfill')
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sanitize temporal data in silver_social_posts."
    )
    p.add_argument(
        "--cutoff-date",
        dest="cutoff_date",
        default=ELECTORAL_CUTOFF_DATE.isoformat(),
        metavar="YYYY-MM-DD",
        help=f"Posts published before this date will be marked as 'backfill'. "
        f"Default: {ELECTORAL_CUTOFF_DATE.isoformat()}.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute the UPDATE. Without this flag, runs a dry-run audit only.",
    )
    p.add_argument(
        "--project",
        default=None,
        help="GCP project ID. Defaults to the environment default.",
    )
    p.add_argument(
        "--dataset",
        default="mapear_silver",
        help="BigQuery dataset containing silver_social_posts. Default: mapear_silver.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    cutoff_date = date.fromisoformat(args.cutoff_date)
    cutoff_iso = datetime(
        cutoff_date.year, cutoff_date.month, cutoff_date.day, tzinfo=timezone.utc
    ).isoformat()
    dataset = args.dataset
    project = args.project
    apply = args.apply

    print(f"Temporal sanitation — cutoff: {cutoff_iso}")
    print(f"Table: {dataset}.{SILVER_TABLE}")
    print(f"Mode: {'APPLY (UPDATE)' if apply else 'DRY RUN (audit only)'}")
    print()

    client = _bq_client(project)

    # --- Audit ---
    print("=== Audit: rows requiring remediation ===")
    audit_rows = list(client.query(_audit_query(dataset, cutoff_iso)).result())

    if not audit_rows:
        print("✓ No pre-cutoff or untagged rows found. Table is clean.")
        return

    total_bad = 0
    for row in audit_rows:
        label = row["data_type"] if row["data_type"] else "NULL"
        print(
            f"  platform={row['platform']:12s}  data_type={label:12s}  "
            f"rows={row['row_count']:>8,d}  "
            f"min={row['min_published_at']}  max={row['max_published_at']}"
        )
        total_bad += row["row_count"]

    print(f"\nTotal rows to remediate: {total_bad:,d}")

    if not apply:
        print(
            "\nDRY RUN — no changes made. "
            "Re-run with --apply to mark these rows as data_type='backfill'."
        )
        sys.exit(1)

    # --- Apply ---
    print("\n=== Applying UPDATE ===")
    update_job = client.query(_update_query(dataset, cutoff_iso))
    update_job.result()
    affected = update_job.num_dml_affected_rows or 0
    print(f"✓ Updated {affected:,d} rows → data_type='backfill'")

    # Verify
    count_row = list(client.query(_count_query(dataset, cutoff_iso)).result())[0]
    remaining = count_row["total"]
    if remaining == 0:
        print("✓ Verification passed — no pre-cutoff or untagged rows remain.")
    else:
        print(
            f"WARNING: {remaining:,d} rows still need remediation after UPDATE. "
            "Check for concurrent writes or query errors.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
