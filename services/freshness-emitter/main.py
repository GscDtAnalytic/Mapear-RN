"""Emit silver/gold freshness as Cloud Monitoring custom metrics.

Queries MAX(since_column) for each tracked table, computes staleness in
minutes, and writes points to custom.googleapis.com/mapear/freshness_minutes
labeled by table. Drives alerts M-01, M-02, M-03 from the 2026-04-18
diagnostic — the incident that motivated this is the only reason these
gauges exist, so keep them dumb and stable.

Runs as a standalone Cloud Run Job (`mapear-freshness-emitter-pipeline`)
scheduled at */30 * * * * by Cloud Scheduler. Built from this directory's
Dockerfile; image published to Artifact Registry as
`southamerica-east1-docker.pkg.dev/your-gcp-project/mapear-rn/freshness-emitter`.

Usage (local):
    GCP_PROJECT_ID=your-gcp-project python main.py
"""

import os
import sys
import time
from datetime import UTC, datetime

from google.cloud import bigquery
from google.cloud import monitoring_v3
from loguru import logger

_LAST_MODIFIED = "_last_modified"

# since_column selection:
#   extracted_at — tables with a real ingestion timestamp (raw + silver + incremental gold)
#   _LAST_MODIFIED — gold tables whose only timestamp is published_at (DATE granularity),
#                    which inflates staleness by up to 24h; uses __TABLES__.last_modified_time
TRACKED_TABLES: list[tuple[str, str]] = [
    # raw — RSS (cadence 8h, threshold group: rss → 960 min)
    ("mapear_raw.raw_articles", "extracted_at"),
    # raw — social (cadence 24h, threshold group: default → 2880 min)
    ("mapear_raw.raw_social_posts_facebook", "extracted_at"),
    ("mapear_raw.raw_social_posts_instagram", "extracted_at"),
    # X: 72h until heartbeat is implemented (see tech_debt_x_freshness_no_heartbeat.md)
    ("mapear_raw.raw_social_posts_x", "extracted_at"),
    ("mapear_raw.raw_social_posts_tiktok", "extracted_at"),
    # silver — written directly by ETL pipelines, not dbt
    ("mapear_silver.silver_articles", "extracted_at"),
    ("mapear_silver.silver_social_posts", "extracted_at"),
    # gold — RSS pipeline writes directly (cadence 8h, threshold group: rss → 960 min)
    ("mapear_gold.gold_articles", _LAST_MODIFIED),
    # gold — dbt marts (cadence 24h, threshold group: default → 2880 min)
    ("mapear_gold.mapear_events", "extracted_at"),
    ("mapear_gold.fct_content", "extracted_at"),
    ("mapear_gold.fct_content_gold", "extracted_at"),
    ("mapear_gold.fct_entity_sentiment", _LAST_MODIFIED),
    ("mapear_gold.fct_trends", _LAST_MODIFIED),
    ("mapear_gold.dim_topics", _LAST_MODIFIED),
]


def compute_staleness_minutes(now: datetime, last_written: datetime) -> float:
    """Return minutes elapsed between ``last_written`` and ``now``.

    Pure function — no I/O. Extracted so unit tests can verify staleness
    arithmetic without a BQ connection.
    """
    return (now - last_written).total_seconds() / 60.0


def _staleness_minutes(
    bq_client: bigquery.Client, table_fqn: str, since_column: str
) -> float | None:
    """Return minutes since ``table_fqn`` was last written.

    For gold tables we use ``__TABLES__.last_modified_time`` because their
    ``published_at`` is DATE-granularity (midnight), which inflates staleness
    by up to 24h regardless of pipeline health. Silver tables keep column-based
    measurement since ``extracted_at`` is a true ingestion timestamp.
    """
    if since_column == _LAST_MODIFIED:
        dataset, table = table_fqn.split(".", 1)
        sql = (
            f"SELECT TIMESTAMP_MILLIS(last_modified_time) AS last_written "
            f"FROM `{dataset}.__TABLES__` WHERE table_id = '{table}'"
        )
    else:
        sql = f"SELECT MAX({since_column}) AS last_written FROM `{table_fqn}`"
    try:
        rows = list(bq_client.query(sql).result())
    except Exception as e:
        logger.error("Freshness query failed for {t}: {err}", t=table_fqn, err=str(e))
        return None

    if not rows:
        return None
    last_written = rows[0][0]
    if last_written is None:
        return None
    return compute_staleness_minutes(datetime.now(UTC), last_written)


def _write_point(
    mon_client: monitoring_v3.MetricServiceClient,
    project_name: str,
    table_fqn: str,
    staleness_minutes: float,
) -> None:
    """Push one point to the freshness_minutes gauge."""
    series = monitoring_v3.TimeSeries()
    series.metric.type = "custom.googleapis.com/mapear/freshness_minutes"
    series.metric.labels["table"] = table_fqn
    series.resource.type = "global"

    now = time.time()
    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {
                "seconds": int(now),
                "nanos": int((now - int(now)) * 1e9),
            }
        }
    )
    point = monitoring_v3.Point(
        {
            "interval": interval,
            "value": {"double_value": float(staleness_minutes)},
        }
    )
    series.points = [point]

    mon_client.create_time_series(name=project_name, time_series=[series])


def main() -> int:
    project_id = os.environ.get("GCP_PROJECT_ID", "").strip()
    if not project_id:
        logger.error("GCP_PROJECT_ID is required")
        return 1

    bq_client = bigquery.Client(project=project_id)
    mon_client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{project_id}"

    failures = 0
    emitted = 0
    for table, since_col in TRACKED_TABLES:
        staleness = _staleness_minutes(bq_client, table, since_col)
        if staleness is None:
            failures += 1
            continue
        _write_point(mon_client, project_name, table, staleness)
        emitted += 1
        logger.info(
            "Emitted freshness for {table}: {staleness:.0f}min",
            table=table,
            staleness=staleness,
        )

    logger.info(
        "Done at {ts}Z — emitted {ok}/{total} ({fail} failures)",
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        ok=emitted,
        total=len(TRACKED_TABLES),
        fail=failures,
    )
    return 1 if failures and emitted == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
