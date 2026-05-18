"""Retry DAG — reprocesses articles from the Dead Letter Queue.

Schedule: once daily at 06:00 UTC
Only retries articles that haven't exceeded max_retries.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

default_args = {
    "owner": "mapear",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
}


def _retry_failed_extractions(**context: object) -> int:
    """Retry failed article extractions from the DLQ."""
    from sqlalchemy import create_engine, text

    from mapear_rss.config import get_rss_settings as get_settings
    from mapear_rss.discovery.url_frontier import URLFrontier
    from mapear_rss.extraction.scraper import Scraper

    settings = get_settings()
    engine = create_engine(settings.postgres.dsn)

    # Buscar artigos falhados com retry disponível
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT url, source_feed
                FROM failed_articles
                WHERE stage = 'extraction'
                  AND resolved_at IS NULL
                  AND retry_count < max_retries
                ORDER BY last_failure_at ASC
                LIMIT 50
            """
            )
        ).fetchall()

    if not rows:
        return 0

    urls = [{"url": row.url, "source_feed": row.source_feed} for row in rows]

    with Scraper() as scraper:
        articles = scraper.scrape_batch(urls)

    # Marcar resolvidos
    frontier = URLFrontier(engine=engine)
    extracted_urls = {str(a.url) for a in articles}

    with engine.begin() as conn:
        for item in urls:
            if item["url"] in extracted_urls:
                conn.execute(
                    text(
                        """
                        UPDATE failed_articles
                        SET resolved_at = NOW()
                        WHERE url = :url AND stage = 'extraction'
                    """
                    ),
                    {"url": item["url"]},
                )
                matching = [a for a in articles if str(a.url) == item["url"]]
                if matching:
                    frontier.mark_completed(item["url"], matching[0].content_hash)

    return len(articles)


with DAG(
    dag_id="mapear_rss_retry_failed",
    default_args=default_args,
    description="Retry failed articles from the Dead Letter Queue",
    schedule="0 6 * * *",
    start_date=datetime(2026, 4, 1),
    catchup=False,
    tags=["mapear", "retry", "dlq"],
    max_active_runs=1,
) as dag:

    retry_task = PythonOperator(
        task_id="retry_failed_extractions",
        python_callable=_retry_failed_extractions,
    )
