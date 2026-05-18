"""Backfill PoliticalSentimentClassifier sobre rss_gold.gold_articles (C3.1 / BL-F2-05).

Roda o classifier polarity-only sobre artigos onde ``sentiment_label IS NULL``
e ``published_at >= today - window-days`` (default 90), preenchendo seis
campos via UPDATE (DuckDB) ou MERGE (BigQuery). Idempotente — re-execução
ignora rows já classificadas.

Uso::

    # Local (DuckDB)
    poetry run python scripts/backfill_political_sentiment.py --window-days 90

    # Prod (BigQuery) — exige GCP creds e GCP_PROJECT_ID
    ENVIRONMENT=production poetry run python scripts/backfill_political_sentiment.py \
        --window-days 90 --project-id your-gcp-project

Pós-execução, validar::

    SELECT COUNT(*) FROM gold_articles
    WHERE published_at >= CURRENT_DATE - 90 AND sentiment_label IS NULL;
    -- esperado: 0
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger

from mapear_infra.config import get_settings
from mapear_infra.logging import setup_logging
from mapear_nlp.political_sentiment import (
    ClassificationResult,
    PoliticalSentimentClassifier,
)

_BATCH_SIZE = 1000


@dataclass(frozen=True)
class _Row:
    content_hash: str
    sentiment_overall: float | None


@dataclass(frozen=True)
class _Update:
    content_hash: str
    sentiment_label: str
    confidence_score: float
    risk_score: float
    decision_factors: list[dict[str, str | float]]
    rule_version: str
    model_version: str

    @classmethod
    def from_classification(
        cls, content_hash: str, result: ClassificationResult
    ) -> _Update:
        return cls(
            content_hash=content_hash,
            sentiment_label=result.label,
            confidence_score=result.confidence,
            risk_score=result.risk_score,
            decision_factors=result.factors_as_dicts(),
            rule_version=result.rule_version,
            model_version=result.model_version,
        )


def _classify_row(classifier: PoliticalSentimentClassifier, row: _Row) -> _Update:
    polarity = row.sentiment_overall or 0.0
    result = classifier.classify(
        polarity=float(polarity),
        volume_24h=0,
        velocity=0.0,
        engagement=0,
    )
    return _Update.from_classification(row.content_hash, result)


def _backfill_duckdb(window_days: int) -> tuple[int, int]:
    """Returns (rows_scanned, rows_updated)."""
    import duckdb

    db_path = get_settings().data_lake_path.parent / "mapear_rn.duckdb"
    if not db_path.exists():
        logger.error("DuckDB not found at {path} — run pipeline first.", path=db_path)
        return 0, 0

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    conn = duckdb.connect(str(db_path))
    classifier = PoliticalSentimentClassifier()

    rows_scanned = 0
    rows_updated = 0

    select_sql = """
        SELECT content_hash, sentiment_overall
        FROM main.gold_articles
        WHERE published_at >= ?
          AND sentiment_label IS NULL
    """
    rows = [
        _Row(content_hash=h, sentiment_overall=s)
        for h, s in conn.execute(select_sql, [cutoff]).fetchall()
    ]
    rows_scanned = len(rows)
    if not rows:
        logger.info("No rows to backfill (window={d}d).", d=window_days)
        return 0, 0

    logger.info(
        "Backfilling {n} DuckDB rows (window={d}d)…", n=rows_scanned, d=window_days
    )
    for row in rows:
        upd = _classify_row(classifier, row)
        conn.execute(
            """
            UPDATE main.gold_articles
            SET sentiment_label = ?,
                confidence_score = ?,
                risk_score = ?,
                rule_version = ?,
                model_version = ?
            WHERE content_hash = ?
            """,
            [
                upd.sentiment_label,
                upd.confidence_score,
                upd.risk_score,
                upd.rule_version,
                upd.model_version,
                upd.content_hash,
            ],
        )
        rows_updated += 1

    return rows_scanned, rows_updated


def _backfill_bq(window_days: int, project_id: str | None) -> tuple[int, int]:
    """BigQuery backfill via temp staging table + MERGE."""
    from google.cloud import bigquery

    project = project_id or os.environ.get("GCP_PROJECT_ID")
    if not project:
        logger.error("GCP_PROJECT_ID not set; pass --project-id explicitly.")
        return 0, 0

    dataset_gold = os.environ.get("GCP_BQ_DATASET_GOLD", "mapear_gold")
    table_gold = f"{project}.{dataset_gold}.gold_articles"
    staging_table = f"{project}.{dataset_gold}._tmp_political_sentiment_backfill"

    client = bigquery.Client(project=project)
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    classifier = PoliticalSentimentClassifier()

    select_sql = f"""
        SELECT content_hash, sentiment_overall
        FROM `{table_gold}`
        WHERE published_at >= @cutoff
          AND sentiment_label IS NULL
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff)],
    )
    rows = [
        _Row(content_hash=r.content_hash, sentiment_overall=r.sentiment_overall)
        for r in client.query(select_sql, job_config=job_config).result()
    ]
    rows_scanned = len(rows)
    if not rows:
        logger.info("No rows to backfill (window={d}d).", d=window_days)
        return 0, 0

    logger.info(
        "Backfilling {n} BigQuery rows (window={d}d)…", n=rows_scanned, d=window_days
    )

    rows_updated = 0
    for chunk_start in range(0, len(rows), _BATCH_SIZE):
        chunk = rows[chunk_start : chunk_start + _BATCH_SIZE]
        updates = [_classify_row(classifier, r) for r in chunk]

        # Stage updates in a temp table, then MERGE.
        staging_schema = [
            bigquery.SchemaField("content_hash", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("sentiment_label", "STRING"),
            bigquery.SchemaField("confidence_score", "FLOAT64"),
            bigquery.SchemaField("risk_score", "FLOAT64"),
            bigquery.SchemaField("rule_version", "STRING"),
            bigquery.SchemaField("model_version", "STRING"),
        ]
        load_job = client.load_table_from_json(
            [
                {
                    "content_hash": u.content_hash,
                    "sentiment_label": u.sentiment_label,
                    "confidence_score": u.confidence_score,
                    "risk_score": u.risk_score,
                    "rule_version": u.rule_version,
                    "model_version": u.model_version,
                }
                for u in updates
            ],
            staging_table,
            job_config=bigquery.LoadJobConfig(
                schema=staging_schema,
                write_disposition="WRITE_TRUNCATE",
            ),
        )
        load_job.result()

        merge_sql = f"""
            MERGE `{table_gold}` T
            USING `{staging_table}` S
            ON T.content_hash = S.content_hash
            WHEN MATCHED AND T.sentiment_label IS NULL THEN
              UPDATE SET
                sentiment_label = S.sentiment_label,
                confidence_score = S.confidence_score,
                risk_score = S.risk_score,
                rule_version = S.rule_version,
                model_version = S.model_version
        """
        client.query(merge_sql).result()
        rows_updated += len(updates)
        logger.info(
            "Merged batch {start}-{end} into gold_articles",
            start=chunk_start,
            end=chunk_start + len(updates),
        )

    # Cleanup staging table.
    client.delete_table(staging_table, not_found_ok=True)
    return rows_scanned, rows_updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days",
        type=int,
        default=90,
        help="Backfill rows where published_at >= today - N days (default 90).",
    )
    parser.add_argument(
        "--project-id",
        type=str,
        default=None,
        help="GCP project id (BQ only). Defaults to $GCP_PROJECT_ID.",
    )
    args = parser.parse_args()

    setup_logging()

    if get_settings().is_local:
        scanned, updated = _backfill_duckdb(args.window_days)
    else:
        scanned, updated = _backfill_bq(args.window_days, args.project_id)

    logger.info(
        "Backfill complete: scanned={s} updated={u} skipped={skip}",
        s=scanned,
        u=updated,
        skip=scanned - updated,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
