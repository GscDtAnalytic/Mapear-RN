#!/usr/bin/env python3
"""
Exporta um JSON piloto dos marts de `your-gcp-project.mapear_gold` para o
arquiteto que vai construir a API.

Fonte única: BigQuery (dbt marts), filtrando apenas conteúdo RN-relevante.
Saída: data/exports/mapear_pilot_<YYYYMMDD>.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery


PROJECT = "your-gcp-project"
DATASET = "mapear_gold"
TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "exports" / f"mapear_pilot_{TODAY}.json"


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
    print(f"=== Mapear-RN pilot export ({TODAY}) ===\n")
    client = bigquery.Client(project=PROJECT)

    # --- Content (RN-relevant only) ---
    print("1. Querying fct_content (RN-relevant)...")
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
        WHERE is_rn_relevant = TRUE
        ORDER BY published_at DESC
    """
    content_rows = rows_to_dicts(client.query(content_sql).result())
    print(f"   {len(content_rows)} conteúdos RN-relevantes")

    # --- Entity sentiment (para aninhar em cada content) ---
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
    """
    sentiment_rows = list(client.query(sentiment_sql).result())
    print(f"   {len(sentiment_rows)} registros de sentimento por entidade")

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

    # --- Trends (agregado por entidade) ---
    print("\n3. Querying fct_trends...")
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
        ORDER BY content_count DESC
    """
    trends_rows = rows_to_dicts(client.query(trends_sql).result())
    print(f"   {len(trends_rows)} entidades agregadas")

    # --- Dimensions ---
    print("\n4. Querying dimensions (cities/mayors, sources, topics)...")
    dim_cities_sql = f"""
        SELECT city, state, population, mayor, party
        FROM `{PROJECT}.{DATASET}.dim_rn_cities_mayors`
        WHERE is_current = TRUE
        ORDER BY population DESC
    """
    dim_sources_sql = f"""
        SELECT source_id, source_name, source_type, channel_id
        FROM `{PROJECT}.{DATASET}.dim_sources`
        ORDER BY source_type, source_name
    """
    dim_topics_sql = f"""
        SELECT topic_id, topics, content_count, avg_sentiment, first_content, last_content
        FROM `{PROJECT}.{DATASET}.dim_topics`
        ORDER BY content_count DESC
    """
    cities = rows_to_dicts(client.query(dim_cities_sql).result())
    sources = rows_to_dicts(client.query(dim_sources_sql).result())
    topics = rows_to_dicts(client.query(dim_topics_sql).result())
    print(f"   cidades={len(cities)}, sources={len(sources)}, topics={len(topics)}")

    # --- Metadata ---
    rss_items = [c for c in content_rows if c["source_type"] == "rss"]

    def date_range(items: list[dict], field: str) -> dict:
        values = [i[field] for i in items if i.get(field)]
        return {
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_project": f"{PROJECT}.{DATASET}",
        "scope": "RN-relevant content only (is_rn_relevant = TRUE)",
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
    }

    payload = {
        "metadata": metadata,
        "dimensions": {
            "rn_cities_mayors": cities,
            "sources": sources,
            "topics": topics,
        },
        "content": content_rows,
        "trends": trends_rows,
    }

    # --- Write ---
    print("\n5. Writing JSON...")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    size_mb = OUTPUT_PATH.stat().st_size / (1024 * 1024)

    print("\n" + "=" * 60)
    print("RESUMO")
    print("=" * 60)
    print(f"  Conteúdo RN-relevante:  {len(content_rows)} (rss={len(rss_items)})")
    print(f"  Sentimento por entidade: {len(sentiment_rows)} registros")
    print(f"  Trends agregadas:       {len(trends_rows)} entidades")
    print(f"  Arquivo:                {OUTPUT_PATH}")
    print(f"  Tamanho:                {size_mb:.2f} MB")


if __name__ == "__main__":
    main()
