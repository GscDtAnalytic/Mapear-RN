"""
Proto-mapping para Fase C2 — NÃO EXECUTAR como backfill de TDT-TOPIC-01.

Este script implementa inferência determinística de topic_id_source via
re-classificação por keyword. Foi rejeitado como mecanismo de backfill
pelo ADR-TDT-TOPIC-01 (introduz falsos positivos de proveniência):

- Falsos positivos de keyword_map: colisão fortuita entre GCP ordinal e
  ID do TOPIC_ID_MAP — indistinguível sem registro de origem original.
- Falsos positivos de gcp_ordinal: keyword rules podem ter mudado desde
  a classificação original; inferência sem prova de estado passado.
- Bug Regime 3 (janela 2026-04-10/11): zeros do bug seriam marcados
  keyword_map ou gcp_ordinal, enterrando o bug no histórico.

Fica preservado como base para o canonical mapping da Fase C2, onde será
aplicado com regras curadas e revisão humana, populando topic_canonical_id
(não topic_id_source).

Usage (Fase C2 apenas, com aprovação explícita):
    poetry run python infra/migrations/c2_canonical_mapping/proto_topic_canonical_inference.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from google.cloud import bigquery
from loguru import logger

PROJECT = "your-gcp-project"
GOLD_TABLE = f"{PROJECT}.mapear_gold.gold_articles"
SILVER_TABLE = f"{PROJECT}.mapear_silver.silver_articles"
TMP_DATASET = f"{PROJECT}.tmp"
TMP_BACKFILL = f"{TMP_DATASET}.tdt_topic_01_backfill"
TMP_RESOLVED = f"{TMP_DATASET}.tdt_topic_01_backfill_resolved"

BASELINE_TOTAL = 871
DIVERGENCE_THRESHOLD = 0.05
TEXT_ABSENT_THRESHOLD = 0.01


@dataclass
class PreConditionResult:
    total: int
    null_source: int
    deterministic_unclassified: int
    requires_reclassification: int
    silver_join_missing: int


def run_pre_conditions(client: bigquery.Client) -> PreConditionResult:
    """Validate row counts against expected baseline before touching anything."""
    query = f"""
    SELECT
        COUNT(*) AS total,
        COUNTIF(topic_id_source IS NULL) AS null_source,
        COUNTIF(topic_id_source IS NULL AND topic_id = -1) AS deterministic_unclassified,
        COUNTIF(topic_id_source IS NULL AND topic_id != -1) AS requires_reclassification
    FROM `{GOLD_TABLE}`
    """
    row = list(client.query(query).result())[0]

    total = row["total"]
    divergence = abs(total - BASELINE_TOTAL) / BASELINE_TOTAL
    if divergence > DIVERGENCE_THRESHOLD:
        logger.warning(
            "Total row count {total} diverges {pct:.1%} from baseline {baseline} "
            "(threshold {threshold:.0%}) — verify before proceeding",
            total=total,
            pct=divergence,
            baseline=BASELINE_TOTAL,
            threshold=DIVERGENCE_THRESHOLD,
        )

    requires_reclassification = row["requires_reclassification"]

    # Check text availability in silver for rows that need reclassification.
    silver_join_missing = 0
    if requires_reclassification > 0:
        join_query = f"""
        SELECT
            COUNTIF(s.content_hash IS NULL) AS missing
        FROM `{GOLD_TABLE}` g
        LEFT JOIN `{SILVER_TABLE}` s USING (content_hash)
        WHERE g.topic_id_source IS NULL AND g.topic_id != -1
        """
        join_row = list(client.query(join_query).result())[0]
        silver_join_missing = join_row["missing"]
        missing_pct = silver_join_missing / requires_reclassification if requires_reclassification else 0
        if missing_pct > TEXT_ABSENT_THRESHOLD:
            logger.error(
                "Text absent in silver for {missing} / {total} rows ({pct:.1%}) — "
                "exceeds {threshold:.0%} threshold. STOPPING.",
                missing=silver_join_missing,
                total=requires_reclassification,
                pct=missing_pct,
                threshold=TEXT_ABSENT_THRESHOLD,
            )
            sys.exit(1)

    result = PreConditionResult(
        total=total,
        null_source=row["null_source"],
        deterministic_unclassified=row["deterministic_unclassified"],
        requires_reclassification=requires_reclassification,
        silver_join_missing=silver_join_missing,
    )
    logger.info(
        "Pre-conditions: total={total}, null_source={null_source}, "
        "deterministic_unclassified={det}, requires_reclassification={reclassify}, "
        "silver_missing={missing}",
        total=result.total,
        null_source=result.null_source,
        det=result.deterministic_unclassified,
        reclassify=result.requires_reclassification,
        missing=result.silver_join_missing,
    )
    return result


def mark_deterministic_unclassified(client: bigquery.Client, dry_run: bool) -> int:
    """UPDATE rows with topic_id=-1 directly to 'unclassified' (no reclassification needed)."""
    query = f"""
    UPDATE `{GOLD_TABLE}`
    SET topic_id_source = 'unclassified'
    WHERE topic_id_source IS NULL AND topic_id = -1
    """
    if dry_run:
        count_query = f"""
        SELECT COUNT(*) AS n FROM `{GOLD_TABLE}`
        WHERE topic_id_source IS NULL AND topic_id = -1
        """
        n = list(client.query(count_query).result())[0]["n"]
        logger.info("[DRY RUN] Would mark {n} rows as unclassified", n=n)
        return n

    job = client.query(query)
    job.result()
    affected = job.num_dml_affected_rows or 0
    logger.info("Marked {n} rows as unclassified", n=affected)
    return affected


def build_temp_backfill_table(client: bigquery.Client, dry_run: bool) -> int:
    """Create temp table joining gold with silver text for rows needing reclassification."""
    query = f"""
    CREATE OR REPLACE TABLE `{TMP_BACKFILL}` AS
    SELECT
        g.content_hash,
        g.topic_id AS topic_id_stored,
        s.content_clean AS source_text
    FROM `{GOLD_TABLE}` g
    JOIN `{SILVER_TABLE}` s USING (content_hash)
    WHERE g.topic_id_source IS NULL
    """
    if dry_run:
        count_query = f"""
        SELECT COUNT(*) AS n
        FROM `{GOLD_TABLE}` g
        JOIN `{SILVER_TABLE}` s USING (content_hash)
        WHERE g.topic_id_source IS NULL
        """
        n = list(client.query(count_query).result())[0]["n"]
        logger.info("[DRY RUN] Would create temp table with {n} rows for reclassification", n=n)
        return n

    client.query(query).result()
    count = list(client.query(f"SELECT COUNT(*) AS n FROM `{TMP_BACKFILL}`").result())[0]["n"]
    logger.info("Temp backfill table created with {n} rows", n=count)
    return count


def reclassify_and_resolve(client: bigquery.Client, dry_run: bool) -> None:
    """Re-classify texts using classify_by_keywords and write resolved sources to temp table."""
    from mapear_nlp.nlp.topic_modeling import classify_by_keywords

    if dry_run:
        logger.info("[DRY RUN] Skipping reclassification pass (no temp table in dry-run)")
        return

    rows = list(client.query(f"SELECT content_hash, topic_id_stored, source_text FROM `{TMP_BACKFILL}`").result())
    if not rows:
        logger.info("No rows to reclassify")
        return

    resolved = []
    for row in rows:
        result = classify_by_keywords(f"{row['source_text']}")
        keyword_topic_id = result["topic_id"]

        # Deterministic rule: keyword agrees with stored value AND is a valid TOPIC_ID_MAP
        # ID (1-10) → KEYWORD_MAP. 0 is a GCP ordinal index, never a keyword map ID.
        if keyword_topic_id == row["topic_id_stored"] and 1 <= keyword_topic_id <= 10:
            resolved_source = "keyword_map"
        else:
            resolved_source = "gcp_ordinal"

        resolved.append(
            {
                "content_hash": row["content_hash"],
                "topic_id_stored": row["topic_id_stored"],
                "keyword_topic_id": keyword_topic_id,
                "resolved_source": resolved_source,
            }
        )

    schema = [
        bigquery.SchemaField("content_hash", "STRING"),
        bigquery.SchemaField("topic_id_stored", "INTEGER"),
        bigquery.SchemaField("keyword_topic_id", "INTEGER"),
        bigquery.SchemaField("resolved_source", "STRING"),
    ]
    table_ref = bigquery.Table(TMP_RESOLVED, schema=schema)
    client.delete_table(TMP_RESOLVED, not_found_ok=True)
    client.create_table(table_ref)
    errors = client.insert_rows_json(TMP_RESOLVED, resolved)
    if errors:
        logger.error("Failed to insert resolved rows: {errors}", errors=errors)
        sys.exit(1)

    keyword_map_count = sum(1 for r in resolved if r["resolved_source"] == "keyword_map")
    gcp_ordinal_count = sum(1 for r in resolved if r["resolved_source"] == "gcp_ordinal")
    logger.info(
        "Resolved {total} rows: keyword_map={km}, gcp_ordinal={go}",
        total=len(resolved),
        km=keyword_map_count,
        go=gcp_ordinal_count,
    )


def apply_resolved_sources(client: bigquery.Client, dry_run: bool) -> int:
    """UPDATE gold_articles with resolved topic_id_source from temp table."""
    query = f"""
    UPDATE `{GOLD_TABLE}` g
    SET topic_id_source = b.resolved_source
    FROM `{TMP_RESOLVED}` b
    WHERE g.content_hash = b.content_hash AND g.topic_id_source IS NULL
    """
    if dry_run:
        logger.info("[DRY RUN] Skipping UPDATE (no resolved table in dry-run)")
        return 0

    job = client.query(query)
    job.result()
    affected = job.num_dml_affected_rows or 0
    logger.info("Updated {n} rows with resolved topic_id_source", n=affected)
    return affected


def validate_post_backfill(client: bigquery.Client) -> None:
    """Verify no NULL topic_id_source remains (modulo in-flight writes)."""
    query = f"""
    SELECT
        topic_id_source,
        COUNT(*) AS n
    FROM `{GOLD_TABLE}`
    GROUP BY 1
    ORDER BY 2 DESC
    """
    rows = list(client.query(query).result())
    logger.info("Post-backfill distribution:")
    null_count = 0
    for row in rows:
        label = row["topic_id_source"] if row["topic_id_source"] is not None else "NULL"
        logger.info("  {source}: {n}", source=label, n=row["n"])
        if row["topic_id_source"] is None:
            null_count = row["n"]

    if null_count > 0:
        logger.warning(
            "{n} rows still have topic_id_source IS NULL — may be in-flight writes",
            n=null_count,
        )
    else:
        logger.info("All rows have topic_id_source populated")


def main() -> None:
    parser = argparse.ArgumentParser(description="TDT-TOPIC-01 backfill script")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    parser.add_argument("--apply", action="store_true", help="Execute UPDATEs")
    parser.add_argument(
        "--i-understand-this-writes-to-prod",
        action="store_true",
        dest="confirmed",
        help="Required confirmation flag when using --apply",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        logger.error("Specify --dry-run or --apply --i-understand-this-writes-to-prod")
        sys.exit(1)

    if args.apply and not args.confirmed:
        logger.error("--apply requires --i-understand-this-writes-to-prod")
        sys.exit(1)

    dry_run = not args.apply

    client = bigquery.Client(project=PROJECT)

    logger.info("=== TDT-TOPIC-01 backfill {'DRY RUN' if dry_run else 'APPLY'} ===")

    pre = run_pre_conditions(client)

    if pre.null_source == 0:
        logger.info("No rows with null topic_id_source — backfill already complete")
        return

    mark_deterministic_unclassified(client, dry_run)
    n = build_temp_backfill_table(client, dry_run)

    if n > 0:
        reclassify_and_resolve(client, dry_run)
        apply_resolved_sources(client, dry_run)

    if not dry_run:
        validate_post_backfill(client)

    logger.info("=== Backfill complete ===")


if __name__ == "__main__":
    main()
