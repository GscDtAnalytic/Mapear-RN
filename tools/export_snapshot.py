#!/usr/bin/env python3
"""
Exporta todos os dados extraídos de todas as pipelines até um momento
(parâmetro --until) para um arquivo JSON.

Fonte: BigQuery prod (your-gcp-project.mapear_gold)
Saída: data/exports/mapear_snapshot_<until_ts>.json

Uso:
    python scripts/export_snapshot.py
    python scripts/export_snapshot.py --until "2026-04-24T23:59:59"
    python scripts/export_snapshot.py --until "2026-04-01" --output /tmp/snapshot.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery

PROJECT = "your-gcp-project"
DATASET = "mapear_gold"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta snapshot de produção para JSON."
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Cutoff ISO8601 (ex: '2026-04-24T23:59:59'). Default: agora.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Caminho do arquivo de saída. Default: data/exports/mapear_snapshot_<ts>.json",
    )
    return parser.parse_args()


def parse_until(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: to_json_safe(v) for k, v in value.items()}
    return value


def rows_to_dicts(rows) -> list[dict]:
    return [{k: to_json_safe(v) for k, v in dict(row).items()} for row in rows]


def main() -> None:
    args = parse_args()
    until = parse_until(args.until)
    until_str = until.strftime("%Y%m%d_%H%M%S")

    output_path = (
        Path(args.output)
        if args.output
        else (PROJECT_ROOT / "data" / "exports" / f"mapear_snapshot_{until_str}.json")
    )

    print("=== Mapear-RN snapshot export ===")
    print(f"  Cutoff (until): {until.isoformat()}")
    print(f"  Destino:        {output_path}\n")

    client = bigquery.Client(project=PROJECT)
    until_bq = until.strftime("%Y-%m-%d %H:%M:%S")

    # --- fct_content: todos os conteúdos extraídos até o cutoff ---
    print("1. Querying fct_content (extracted_at <= until)...")
    content_sql = f"""
        SELECT
            content_id,
            source_type,
            title,
            url,
            source_feed,
            channel_name,
            author,
            content_text,
            published_at,
            extracted_at,
            is_rn_relevant,
            mentioned_cities,
            mentioned_mayors,
            mentioned_governors,
            mentioned_parties
        FROM `{PROJECT}.{DATASET}.fct_content`
        WHERE extracted_at <= TIMESTAMP('{until_bq}')
        ORDER BY extracted_at DESC
    """
    content_rows = rows_to_dicts(client.query(content_sql).result())
    rss_items = [r for r in content_rows if r["source_type"] == "rss"]
    print(f"   {len(content_rows)} conteúdos (rss={len(rss_items)})")

    # --- fct_entity_sentiment: filtrado pelos content_ids acima ---
    print("\n2. Querying fct_entity_sentiment...")
    sentiment_sql = f"""
        SELECT
            content_id,
            source_type,
            entity,
            entity_type,
            sentiment,
            mention_count
        FROM `{PROJECT}.{DATASET}.fct_entity_sentiment`
        WHERE extracted_at <= TIMESTAMP('{until_bq}')
    """
    # fct_entity_sentiment pode não ter extracted_at — fallback via JOIN
    try:
        sentiment_rows = list(client.query(sentiment_sql).result())
    except Exception:
        sentiment_sql_fallback = f"""
            SELECT
                s.content_id,
                s.source_type,
                s.entity,
                s.entity_type,
                s.sentiment,
                s.mention_count
            FROM `{PROJECT}.{DATASET}.fct_entity_sentiment` s
            INNER JOIN `{PROJECT}.{DATASET}.fct_content` c
              ON s.content_id = c.content_id AND s.source_type = c.source_type
            WHERE c.extracted_at <= TIMESTAMP('{until_bq}')
        """
        sentiment_rows = list(client.query(sentiment_sql_fallback).result())

    print(f"   {len(sentiment_rows)} registros de sentimento")

    sentiment_by_content: dict[tuple[str, str], list[dict]] = {}
    for r in sentiment_rows:
        key = (r["content_id"], r["source_type"])
        sentiment_by_content.setdefault(key, []).append(
            {
                "entity": r["entity"],
                "entity_type": r["entity_type"],
                "sentiment": r["sentiment"],
                "mention_count": r["mention_count"],
            }
        )

    for c in content_rows:
        entities = sentiment_by_content.get((c["content_id"], c["source_type"]), [])
        overall = (
            round(sum(e["sentiment"] for e in entities) / len(entities), 4)
            if entities
            else None
        )
        c["sentiment"] = {"overall": overall, "by_entity": entities}

    # --- fct_trends: limitado ao período coberto ---
    print("\n3. Querying fct_trends (período até cutoff)...")
    trends_sql = f"""
        SELECT
            entity,
            entity_type,
            total_mentions,
            content_count,
            avg_sentiment,
            min_sentiment,
            max_sentiment,
            first_mention,
            last_mention,
            source_count
        FROM `{PROJECT}.{DATASET}.fct_trends`
        WHERE first_mention <= TIMESTAMP('{until_bq}')
        ORDER BY content_count DESC
    """
    trends_rows = rows_to_dicts(client.query(trends_sql).result())
    print(f"   {len(trends_rows)} entidades")

    # --- Dimensions ---
    print("\n4. Querying dimensions...")
    cities = rows_to_dicts(
        client.query(
            f"""
        SELECT city, state, population, mayor, party
        FROM `{PROJECT}.{DATASET}.dim_rn_cities_mayors`
        WHERE is_current = TRUE
        ORDER BY population DESC
    """
        ).result()
    )

    sources = rows_to_dicts(
        client.query(
            f"""
        SELECT source_id, source_name, source_type, channel_id
        FROM `{PROJECT}.{DATASET}.dim_sources`
        ORDER BY source_type, source_name
    """
        ).result()
    )

    topics = rows_to_dicts(
        client.query(
            f"""
        SELECT topic_id, topics, content_count, avg_sentiment, first_content, last_content
        FROM `{PROJECT}.{DATASET}.dim_topics`
        WHERE first_content <= TIMESTAMP('{until_bq}')
        ORDER BY content_count DESC
    """
        ).result()
    )

    print(f"   cidades={len(cities)}, sources={len(sources)}, topics={len(topics)}")

    # --- Metadata ---
    def date_range(items: list[dict], field: str) -> dict:
        values = [i[field] for i in items if i.get(field)]
        return {
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }

    payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "until": until.isoformat(),
            "source_project": f"{PROJECT}.{DATASET}",
            "schema_version": 1,
            "counts": {
                "total_content": len(content_rows),
                "rss": len(rss_items),
                "entity_sentiment_records": len(sentiment_rows),
                "trends_entities": len(trends_rows),
                "rn_cities_monitored": len(cities),
                "sources": len(sources),
                "topics": len(topics),
            },
            "freshness": {
                "rss_published": date_range(rss_items, "published_at"),
                "rss_extracted": date_range(rss_items, "extracted_at"),
            },
        },
        "dimensions": {
            "rn_cities_mayors": cities,
            "sources": sources,
            "topics": topics,
        },
        "content": content_rows,
        "trends": trends_rows,
    }

    # --- Write ---
    print("\n5. Escrevendo JSON...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    size_mb = output_path.stat().st_size / (1024 * 1024)

    print("\n" + "=" * 60)
    print("RESUMO")
    print("=" * 60)
    print(f"  Cutoff:          {until.isoformat()}")
    print(f"  Conteúdo total:  {len(content_rows)} (rss={len(rss_items)})")
    print(f"  Sentimento:      {len(sentiment_rows)} registros")
    print(f"  Trends:          {len(trends_rows)} entidades")
    print(f"  Arquivo:         {output_path}")
    print(f"  Tamanho:         {size_mb:.2f} MB")


if __name__ == "__main__":
    main()
