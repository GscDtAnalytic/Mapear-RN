"""Mapear-RSS — Main pipeline DAG.

Schedule: every 8 hours (0 */8 * * *)
Flow: discover → extract → transform_silver → dbt_run → quality_checks → notify

Uses TaskGroups for logical isolation. Each group writes state
to the data lake so re-runs don't reprocess earlier stages.
"""

from datetime import UTC, datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.task_group import TaskGroup

default_args = {
    "owner": "mapear",
    "depends_on_past": False,
    "email_on_failure": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


def _discover_urls(**context: object) -> list[str]:
    """Fetch RSS feeds and add new URLs to the frontier."""
    from sqlalchemy import create_engine, text

    from mapear_rss.config import get_rss_settings as get_settings
    from mapear_rss.discovery.rss_reader import RSSReader
    from mapear_rss.discovery.url_frontier import URLFrontier

    settings = get_settings()
    engine = create_engine(settings.postgres.dsn)

    # Buscar feeds ativos do banco
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT url FROM feed_sources WHERE is_active = TRUE")
        ).fetchall()
    feed_urls = [row.url for row in rows]

    if not feed_urls:
        from loguru import logger

        logger.warning("No active feeds found. Run 'make seed-feeds' first.")
        return []

    reader = RSSReader()
    discovered = reader.fetch_multiple(feed_urls)

    frontier = URLFrontier(engine=engine)
    inserted = frontier.add_urls(discovered)

    return [str(u.url) for u in discovered[:inserted]]


def _extract_articles(**context: object) -> int:
    """Scrape pending URLs from the frontier."""
    import pandas as pd
    from sqlalchemy import create_engine

    from mapear_rss.config import get_rss_settings as get_settings
    from mapear_rss.discovery.url_frontier import URLFrontier
    from mapear_rss.extraction.scraper import Scraper
    from mapear_storage.loaders.factory import get_storage_writer

    settings = get_settings()
    engine = create_engine(settings.postgres.dsn)
    frontier = URLFrontier(engine=engine)

    pending = frontier.get_pending(limit=settings.scraper.max_workers * 20)
    if not pending:
        return 0

    with Scraper() as scraper:
        articles = scraper.scrape_batch(pending)

    # Marcar URLs como completed/failed
    extracted_urls = {str(a.url) for a in articles}
    for item in pending:
        if item["url"] in extracted_urls:
            matching = [a for a in articles if str(a.url) == item["url"]]
            if matching:
                frontier.mark_completed(item["url"], matching[0].content_hash)
        else:
            frontier.mark_failed(item["url"])
            frontier.add_to_dlq(
                url=item["url"],
                source_feed=item["source_feed"],
                error_type="extraction_failed",
                error_message="No content extracted",
                stage="extraction",
            )

    # Salvar raw no data lake
    if articles:
        batch_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        df = pd.DataFrame([a.model_dump(mode="json") for a in articles])
        writer = get_storage_writer()
        writer.write_parquet(df, "raw", f"batch={batch_id}")

    return len(articles)


def _transform_silver(**context: object) -> int:
    """Clean, deduplicate, and run NER on raw articles."""
    import glob

    import pandas as pd

    from mapear_domain.models.base import RawArticle
    from mapear_nlp.ner import NERExtractor
    from mapear_rss.config import get_rss_settings as get_settings
    from mapear_rss.transformation.deduplicator import Deduplicator
    from mapear_storage.loaders.factory import get_storage_writer

    settings = get_settings()
    raw_path = settings.lake_raw

    # Ler últimos Parquet raw
    parquet_files = sorted(glob.glob(str(raw_path / "**/*.parquet"), recursive=True))
    if not parquet_files:
        return 0

    # Usar último batch
    latest = parquet_files[-1]
    df = pd.read_parquet(latest)

    raw_articles = [RawArticle(**row) for row in df.to_dict("records")]

    # Deduplicar
    dedup = Deduplicator()
    unique = dedup.deduplicate(raw_articles)

    # NER
    ner = NERExtractor()
    silver_articles = ner.extract_batch(unique)

    # Salvar silver
    if silver_articles:
        batch_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        df_silver = pd.DataFrame([a.model_dump(mode="json") for a in silver_articles])
        writer = get_storage_writer()
        writer.write_parquet(df_silver, "silver", f"batch={batch_id}")

    return len(silver_articles)


def _run_dbt(**context: object) -> None:
    """Execute dbt build (seed + run + test)."""
    import subprocess

    from mapear_rss.config import get_rss_settings as get_settings

    settings = get_settings()
    target = "dev" if settings.is_local else "prod"

    subprocess.run(
        ["dbt", "build", "--target", target],
        cwd="dbt",
        check=True,
    )


def _enrich_gold(**context: object) -> int:
    """Run sentiment analysis and topic modeling on silver articles."""
    import glob

    import pandas as pd

    from mapear_domain.models.base import GoldArticle, SilverArticle
    from mapear_domain.region import load_region
    from mapear_nlp.sentiment import SentimentAnalyzer
    from mapear_nlp.topic_modeling import TopicModeler
    from mapear_nlp.trend_scorer import TrendScorer
    from mapear_rss.config import get_rss_settings as get_settings
    from mapear_storage.loaders.factory import get_storage_writer

    settings = get_settings()
    silver_path = settings.lake_silver

    parquet_files = sorted(glob.glob(str(silver_path / "**/*.parquet"), recursive=True))
    if not parquet_files:
        return 0

    latest = parquet_files[-1]
    df = pd.read_parquet(latest)
    silver_articles = [SilverArticle(**row) for row in df.to_dict("records")]

    # Filtrar apenas RN-relevant para enrichment completo
    rn_articles = [a for a in silver_articles if a.is_rn_relevant]

    from mapear_infra.cache import ContentCache

    sentiment_analyzer = SentimentAnalyzer()
    topic_modeler = TopicModeler()
    trend_scorer = TrendScorer()
    region = load_region(settings.mapear_region)
    entities = list(region.get_city_names() | region.get_mayor_names())
    trend_scores = trend_scorer.score_batch(entities, rn_articles)

    # Use content cache to skip already-enriched articles
    try:
        cache = ContentCache()
    except Exception:
        cache = None

    cached_sentiments: dict[str, dict] = {}
    to_analyze = []
    for article in rn_articles:
        if cache is not None:
            cached = cache.get(article.content_hash)
            if cached is not None:
                cached_sentiments[article.content_hash] = cached
                continue
        to_analyze.append(article)

    if to_analyze:
        new_sentiments = sentiment_analyzer.analyze_batch(to_analyze)
        for article, sent in zip(to_analyze, new_sentiments, strict=True):
            cached_sentiments[article.content_hash] = sent
            if cache is not None:
                cache.set(article.content_hash, sent)

    sentiments = [cached_sentiments[a.content_hash] for a in rn_articles]
    topics = topic_modeler.fit_transform(rn_articles)

    # Build gold articles
    gold_articles = []
    for article, sent, topic in zip(rn_articles, sentiments, topics, strict=False):
        trend = max(
            (trend_scores.get(c, 0) for c in article.mentioned_cities),
            default=0.0,
        )
        gold = GoldArticle(
            url=article.url,
            source_feed=article.source_feed,
            title=article.title,
            content_clean=article.content_clean,
            published_at=article.published_at,
            content_hash=article.content_hash,
            is_rn_relevant=True,
            sentiment_overall=sent["sentiment_overall"],
            sentiment_by_entity=sent["sentiment_by_entity"],
            topics=topic["topics"],
            topic_id=topic["topic_id"],
            trend_score=trend,
        )
        gold_articles.append(gold)

    if gold_articles:
        batch_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        df_gold = pd.DataFrame([a.model_dump(mode="json") for a in gold_articles])
        writer = get_storage_writer()
        writer.write_parquet(df_gold, "gold", f"batch={batch_id}")

    return len(gold_articles)


def _check_source_freshness(**context: object) -> None:
    """Run dbt source freshness checks."""
    import subprocess

    from loguru import logger

    from mapear_rss.config import get_rss_settings as get_settings

    settings = get_settings()
    target = "dev" if settings.is_local else "prod"

    result = subprocess.run(
        ["dbt", "source", "freshness", "--target", target],
        cwd="dbt",
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(
            "Source freshness check returned warnings/errors: {stderr}",
            stderr=result.stderr,
        )
        # Warn but don't block — freshness issues are non-fatal
    else:
        logger.info("All source freshness checks passed")


def _quality_checks(**context: object) -> None:
    """Run data quality checks on the latest batch of each layer."""
    import glob

    import pandas as pd
    from loguru import logger

    from mapear_infra.quality import validate_gold, validate_raw, validate_silver
    from mapear_rss.config import get_rss_settings as get_settings

    settings = get_settings()
    results = {}

    for layer, validator in [
        ("raw", validate_raw),
        ("silver", validate_silver),
        ("gold", validate_gold),
    ]:
        layer_path = getattr(settings, f"lake_{layer}")
        parquet_files = sorted(
            glob.glob(str(layer_path / "**/*.parquet"), recursive=True)
        )
        if parquet_files:
            df = pd.read_parquet(parquet_files[-1])
            results[layer] = validator(df)
        else:
            results[layer] = True  # No data to validate

    failed = [layer for layer, ok in results.items() if not ok]
    if failed:
        raise RuntimeError(
            f"Quality checks FAILED for layers: {failed}. "
            "Pipeline halted to prevent bad data propagation."
        )
    logger.info("All quality checks passed")


def _notify(**context: object) -> None:
    """Send pipeline completion notification via Slack."""
    from loguru import logger

    from mapear_infra.notifier import notify_slack

    ti = context.get("ti")
    discovered = ti.xcom_pull(task_ids="discovery.discover_urls") if ti else 0
    extracted = ti.xcom_pull(task_ids="extraction.extract_articles") if ti else 0
    batch_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    notify_slack(
        discovered=len(discovered) if isinstance(discovered, list) else 0,
        extracted=extracted or 0,
        batch_id=batch_id,
    )
    logger.info("Pipeline batch completed — notification sent")


with DAG(
    dag_id="mapear_rss_pipeline",
    default_args=default_args,
    description="Mapear-RSS: discover, extract, transform, and load news articles",
    schedule="0 */8 * * *",
    start_date=datetime(2026, 4, 1),
    catchup=False,
    tags=["mapear", "rss", "pipeline"],
    max_active_runs=1,
) as dag:

    with TaskGroup("discovery") as tg_discovery:
        discover = PythonOperator(
            task_id="discover_urls",
            python_callable=_discover_urls,
        )

    with TaskGroup("extraction") as tg_extraction:
        extract = PythonOperator(
            task_id="extract_articles",
            python_callable=_extract_articles,
        )

    with TaskGroup("silver_transform") as tg_silver:
        transform = PythonOperator(
            task_id="transform_silver",
            python_callable=_transform_silver,
        )

    with TaskGroup("gold_enrichment") as tg_gold:
        enrich = PythonOperator(
            task_id="enrich_gold",
            python_callable=_enrich_gold,
        )

    with TaskGroup("dbt_run") as tg_dbt:
        dbt = PythonOperator(
            task_id="dbt_build",
            python_callable=_run_dbt,
        )

    with TaskGroup("source_freshness") as tg_freshness:
        freshness = PythonOperator(
            task_id="check_source_freshness",
            python_callable=_check_source_freshness,
        )

    with TaskGroup("quality_checks") as tg_quality:
        quality = PythonOperator(
            task_id="run_quality_checks",
            python_callable=_quality_checks,
        )

    with TaskGroup("notify") as tg_notify:
        notify = PythonOperator(
            task_id="send_notification",
            python_callable=_notify,
        )

    (
        tg_discovery
        >> tg_extraction
        >> tg_silver
        >> tg_gold
        >> tg_dbt
        >> tg_freshness
        >> tg_quality
        >> tg_notify
    )
