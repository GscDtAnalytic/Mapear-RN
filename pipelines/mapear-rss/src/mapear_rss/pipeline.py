"""Local pipeline entry point — runs the full RSS pipeline without Airflow.

Usage:
    ENVIRONMENT=local python -m mapear_rss
    ENVIRONMENT=local python -m mapear_rss --backfill-start-date 2025-01-01
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from loguru import logger

from mapear_domain.entity_resolution import PersonResolver, set_targets_seed_path
from mapear_domain.models.base import GoldArticle, RawArticle, SilverArticle
from mapear_domain.models.shadow import SilverEventShadow
from mapear_domain.region import load_region
from mapear_infra.audit import log_llm_call
from mapear_infra.cache import ContentCache
from mapear_infra.logging import setup_logging
from mapear_infra.metrics import (
    bq_load_failures,
    frontier_queue_depth,
    quality_gate_failures,
    start_metrics_server,
)
from mapear_infra.privacy import parse_level
from mapear_infra.quality import (
    generate_quality_report,
    validate_gold,
    validate_raw,
    validate_silver,
)
from mapear_infra.tracing import setup_tracing
from mapear_infra.watermark import WatermarkManager
from mapear_nlp.llm.client import LLMError, get_llm_client
from mapear_nlp.narrative_cache import NarrativeCache
from mapear_nlp.narrative_explainer import NarrativeExplainer
from mapear_nlp.ner import NERExtractor
from mapear_nlp.political_sentiment import PoliticalSentimentClassifier
from mapear_nlp.sentiment import SentimentAnalyzer
from mapear_nlp.shadow import ShadowScorer, build_shadow_scorer
from mapear_nlp.topic_modeling import TopicModeler
from mapear_nlp.trend_scorer import TrendScorer
from mapear_rss import __version__ as PIPELINE_VERSION  # noqa: N812
from mapear_rss.analysis.diversity_scorer import DiversityScorer
from mapear_rss.config import get_rss_settings
from mapear_rss.discovery.rss_reader import RSSReader
from mapear_rss.discovery.url_frontier import URLFrontier
from mapear_rss.extraction.diagnostics import FetchCounters
from mapear_rss.extraction.scraper import Scraper
from mapear_rss.monitoring.feed_health import FeedHealthMonitor
from mapear_rss.reach_signals import compute_rss_reach_per_person
from mapear_rss.transformation.deduplicator import Deduplicator
from mapear_storage.loaders.factory import (
    get_iceberg_writer,
    get_pubsub_publisher,
    get_storage_writer,
    get_warehouse_loader,
)
from mapear_storage.loaders.parquet_writer import (
    EVENT_SHADOW_SCHEMA,
    GOLD_ARTICLE_SCHEMA,
    RAW_ARTICLE_SCHEMA,
    SILVER_ARTICLE_SCHEMA,
    records_to_dataframe,
    write_dataframe_as_parquet,
)

_UOL_FEED_URL = "https://rss.uol.com.br/feed/noticias.xml"
_FIRST_RUN_LOOKBACK_HOURS = 8  # matches Cloud Scheduler cron (every 8h)


def _load_to_warehouse(
    warehouse,
    uri: str,
    target_table: str,
    failed_loads: list[str],
    *,
    merge_key: str | None = None,
) -> None:
    """Load a GCS parquet URI into BigQuery.

    When ``merge_key`` is set, the loader upserts by that key instead of
    appending — eliminates cross-run duplicates by design.

    Failures are logged and recorded in ``failed_loads`` so the caller can
    surface a non-zero exit code at the end of the run. Silent failures
    were the root cause of incident 2026-04-18 (warehouse frozen 17h).
    """
    if warehouse is None:
        return
    try:
        warehouse.load(uri, target_table, merge_key=merge_key)
    except Exception as e:
        failed_loads.append(target_table)
        bq_load_failures.labels(target_table=target_table).inc()
        logger.error(
            "BQ load failed for {table}: {err}",
            table=target_table,
            err=str(e),
        )


def _classify_political_sentiment(
    gold_articles: list[GoldArticle],
    shadow_scorer: ShadowScorer | None = None,
) -> list[SilverEventShadow]:
    """Apply the FAVORABLE/WARNING/ALERT overlay to RSS gold articles in place.

    Reach signals come from a single-batch aggregation per ``person_id``
    via :func:`mapear_rss.reach_signals.compute_rss_reach_per_person` —
    see that module for the approximation note. RSS has no engagement
    signal so ALERT requires a volume spike + sustained velocity within
    the batch (which is what "many newsrooms ran a critical piece in a
    short window" looks like). Articles without ``person_id`` keep the
    polarity-only path (zero reach), which still lets FAVORABLE and
    default-WARNING fire from polarity alone.

    Stage 1E v2: when ``shadow_scorer`` is set, every event is also
    classified under the candidate thresholds and a ``SilverEventShadow``
    row is produced. The primary path is untouched — the same primary
    ``ClassificationResult`` already in hand is passed to the scorer, so
    the candidate run is the only extra classifier call.
    """
    if not gold_articles:
        return []
    reach = compute_rss_reach_per_person(gold_articles)
    classifier = PoliticalSentimentClassifier()
    shadow_rows: list[SilverEventShadow] = []
    for gold in gold_articles:
        polarity = gold.sentiment_overall or 0.0
        if gold.person_id and gold.person_id in reach:
            volume_24h, velocity, engagement = reach[gold.person_id]
        else:
            volume_24h, velocity, engagement = 0, 0.0, 0
        result = classifier.classify(
            polarity=float(polarity),
            volume_24h=volume_24h,
            velocity=velocity,
            engagement=engagement,
        )
        gold.sentiment_label = result.label
        gold.confidence_score = result.confidence
        gold.risk_score = result.risk_score
        gold.decision_factors = result.factors_as_dicts()
        gold.rule_version = result.rule_version
        gold.model_version = result.model_version

        if shadow_scorer is not None:
            shadow_rows.append(
                shadow_scorer.score(
                    content_hash=gold.content_hash,
                    polarity=float(polarity),
                    volume_24h=volume_24h,
                    velocity=velocity,
                    engagement=engagement,
                    primary=result,
                    person_id=gold.person_id,
                    ingestion_run_id=gold.ingestion_run_id,
                )
            )
    return shadow_rows


def _build_narrative_explainer(settings) -> NarrativeExplainer | None:  # noqa: ANN001
    """Return a configured explainer or None when LLM is unconfigured.

    Eixo 2 v1. The explainer is opt-in — pipelines run unchanged when
    no API key is set (local smoke tests, CI). Bucket-less deployments
    skip the cache; non-fatal.
    """
    cfg = settings.llm
    if not cfg.api_key and not cfg.api_key_secret:
        logger.info(
            "LLM provider unconfigured (no MAPEAR_LLM_API_KEY or _SECRET) — "
            "narrative explainer disabled"
        )
        return None
    try:
        client = get_llm_client(cfg)
    except LLMError as exc:
        logger.warning(
            "Could not build LLM client, narrative explainer disabled: {err}",
            err=exc,
        )
        return None
    cache: NarrativeCache | None = None
    if cfg.cache_enabled and settings.gcp.gcs_bucket_name and settings.gcp.project_id:
        try:
            cache = NarrativeCache.build(
                bucket_name=settings.gcp.gcs_bucket_name,
                project_id=settings.gcp.project_id,
                prefix=cfg.cache_gcs_prefix,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not build narrative cache, running uncached: {err}", err=exc
            )
    redaction_level = parse_level(cfg.pii_level)
    hmac_key = cfg.pii_hmac_key.encode("utf-8") if cfg.pii_hmac_key else None
    return NarrativeExplainer(
        llm_client=client,
        cache=cache,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        timeout_seconds=cfg.timeout_seconds,
        redaction_level=redaction_level,
        hmac_key=hmac_key,
    )


def _apply_narrative_explainer(
    gold_articles: list[GoldArticle],
    silver_index: dict[str, SilverArticle],
    explainer: NarrativeExplainer | None,
    *,
    tenant_id: str | None,
    region_id: str,
    provider: str,
    model: str,
    coverage: str = "alert",
) -> None:
    """Stamp narrative_summary on rows in-place + emit audit log.

    Eixo 2 v1 gates the LLM on ``sentiment_label == "ALERT"`` (or strongly
    negative polarity) so the bill stays bounded (~5% of pipeline rows).
    With ``coverage="all"`` the gate is lifted and every article is
    summarised — positive and neutral narratives included. ``silver_index``
    is keyed by ``content_hash`` so we can read the full article text (Gold
    only keeps cleaned content). Each call (including cache hits and
    failures) emits one structured audit-log line per Eixo 6 light.
    """
    if explainer is None or not gold_articles:
        return
    alert_count = 0
    explained = 0
    for gold in gold_articles:
        # ALERT gate (Eixo 2 v1): originally gated on sentiment_label==ALERT
        # only. Extended to also cover polarity ≤ -0.35 directly because RN's
        # news volume (~2-3 articles/8h/person) rarely produces the volume
        # spike needed for ALERT, but strongly-negative articles warrant a
        # summary regardless of reach signals. coverage="all" lifts the gate
        # entirely (Eixo 2 v2d — positive/neutral narratives also summarised).
        polarity = gold.sentiment_overall or 0.0
        is_very_negative = polarity <= -0.35
        if (
            coverage != "all"
            and gold.sentiment_label != "ALERT"
            and not is_very_negative
        ):
            continue
        alert_count += 1
        silver = silver_index.get(gold.content_hash)
        if silver is None:
            continue
        person_name = (
            gold.mentioned_persons[0]
            if gold.mentioned_persons
            else "(não identificado)"
        )
        # decision_factors_block uses velocity / volume / engagement signals.
        velocity = next(
            (
                f.get("value", 0.0)
                for f in gold.decision_factors
                if f.get("name") == "velocity"
            ),
            0.0,
        )
        volume = next(
            (
                f.get("value", 0)
                for f in gold.decision_factors
                if f.get("name") == "volume"
            ),
            0,
        )
        result = explainer.explain(
            content_hash=gold.content_hash,
            title=silver.title,
            content=silver.content_clean,
            person_name=person_name,
            person_role="",
            polarity=gold.sentiment_overall or 0.0,
            velocity=float(velocity),
            volume=int(volume),
            decision_factors=gold.decision_factors,
            rule_version=gold.rule_version or "",
        )
        if result.summary:
            gold.narrative_summary = result.summary
            gold.narrative_prompt_version = result.prompt_version
            explained += 1
        if result.cache_hit:
            audit_status = "cache_hit"
        elif result.error:
            audit_status = "cache_miss_error"
        else:
            audit_status = "cache_miss_ok"
        log_llm_call(
            tenant_id=tenant_id,
            region=region_id,
            content_hash=gold.content_hash,
            prompt_version=result.prompt_version,
            provider=provider,
            model=model,
            redaction_level=result.redaction_level,
            redaction_counts=result.redaction_counts or {},
            status=audit_status,
            error=result.error,
        )
    logger.info(
        "Narrative explainer: {explained}/{alerts} ALERT rows summarised",
        explained=explained,
        alerts=alert_count,
    )


def _write_shadow_rows(
    shadow_rows: list[SilverEventShadow],
    writer,  # noqa: ANN001
    warehouse,  # noqa: ANN001
    *,
    bq_dataset_silver: str,
    batch_id: str,
    failed_loads: list[str],
) -> None:
    """Stage 4.5b: persist shadow A/B rows to silver_event_shadow.

    Loaded additively (no merge_key) — the grain is
    ``(content_hash, shadow_rule_version)``, a composite the BQ loader's
    single-column merge can't express. Cross-run dedup happens in the
    staging view, mirroring silver_article_stances. tenant_id is already
    stamped by the scorer, so no _stamp_tenant_id pass is needed here.
    """
    if not shadow_rows:
        return
    df_shadow = records_to_dataframe(shadow_rows)
    shadow_uri = write_dataframe_as_parquet(
        writer,
        df_shadow,
        EVENT_SHADOW_SCHEMA,
        "silver",
        f"batch={batch_id}",
    )
    _load_to_warehouse(
        warehouse,
        shadow_uri,
        f"{bq_dataset_silver}.silver_event_shadow",
        failed_loads,
    )
    logger.info(
        "Stage 4.5b shadow: {n} rows written to silver_event_shadow",
        n=len(shadow_rows),
    )


def _write_silver_to_iceberg(df_silver, iceberg_writer, batch_id: str) -> None:
    """Stage 3.7: write silver articles to Iceberg — Eixo 1 v1 (opt-in).

    Runs only when MAPEAR_ICEBERG_ENABLED=true. Never raises so the
    existing BQ write path is unaffected on failure.
    """
    if iceberg_writer is None:
        return
    try:
        import pyarrow as pa

        table = pa.Table.from_pandas(df_silver, preserve_index=False)
        iceberg_writer.append(table, "silver_articles")
        logger.info(
            "Stage 3.7 Iceberg: {rows} silver rows written (batch={batch})",
            rows=len(df_silver),
            batch=batch_id,
        )
    except Exception as exc:
        logger.error(
            "Stage 3.7 Iceberg write failed (batch={batch}): {err}",
            batch=batch_id,
            err=exc,
        )


def _silver_to_gold(
    article: SilverArticle,
    sent: dict,
    topic: dict,
    trend_score: float,
) -> GoldArticle:
    """Build a GoldArticle from a Silver-stage article + enrichments.

    Propaga o overlay eleitoral (`person_id`, `scope_status`) preenchido
    pelo PersonResolver no Stage 3 — sem isso, o gate IN_SCOPE de
    `fct_content_gold` descarta 100% do conteúdo RSS (TDT-RSS-PERSON-01).
    Lineage (`ingestion_run_id`, `pipeline_version`) é propagado de
    SilverArticle para permitir trace de qual run produziu cada linha
    (TDT-RSS-LINEAGE).
    """
    return GoldArticle(
        url=article.url,
        source_feed=article.source_feed,
        title=article.title,
        content_clean=article.content_clean,
        published_at=article.published_at,
        content_hash=article.content_hash,
        is_rn_relevant=True,
        mentioned_cities=article.mentioned_cities,
        mentioned_mayors=article.mentioned_mayors,
        mentioned_governors=article.mentioned_governors,
        mentioned_parties=article.mentioned_parties,
        mentioned_persons=article.mentioned_persons,
        sentiment_overall=sent["sentiment_overall"],
        sentiment_by_entity=sent["sentiment_by_entity"],
        topics=topic["topics"],
        topic_id=topic["topic_id"],
        topic_label=topic.get("topic_label", ""),
        topic_id_source=topic.get("topic_id_source"),
        topic_label_raw=topic.get("topic_label_raw"),
        trend_score=trend_score,
        source_type="rss",
        person_id=article.person_id,
        scope_status=article.scope_status,
        ingestion_run_id=article.ingestion_run_id,
        pipeline_version=article.pipeline_version,
    )


def _stamp_tenant_id(df, tenant_id):  # type: ignore[no-untyped-def]
    """Overwrite the ``tenant_id`` column with the active tenant.

    Stage 2B v1 data plane: every row written from this pipeline gets
    stamped with ``settings.mapear_tenant_id`` right before the parquet
    write. The Pydantic models default ``tenant_id`` to None, so this is
    where the actual value lands. No-op when the env var is unset
    (single-tenant deployment / legacy).
    """
    if tenant_id is None:
        return df
    df = df.copy()
    df["tenant_id"] = tenant_id
    return df


def _deactivate_uol_feed(engine) -> None:  # noqa: ANN001
    """One-time migration: deactivate UOL feed and mark its URLs as failed.

    UOL returns HTTP 403 for all article scraping and is not RN-specific.
    This runs idempotently on each pipeline execution until the feed is gone.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE feed_sources SET is_active = FALSE, updated_at = NOW() "
                "WHERE url = :url AND is_active = TRUE"
            ),
            {"url": _UOL_FEED_URL},
        )
        if result.rowcount > 0:
            logger.info("Deactivated UOL feed in feed_sources")

        result = conn.execute(
            text(
                "UPDATE url_frontier SET status = 'failed', attempt_count = 99, "
                "updated_at = NOW() "
                "WHERE source_feed = :url AND status IN ('pending', 'in_progress')"
            ),
            {"url": _UOL_FEED_URL},
        )
        if result.rowcount > 0:
            logger.info(
                "Marked {count} UOL frontier URLs as permanently failed",
                count=result.rowcount,
            )


def _validate_required_env_vars() -> None:
    """Validate that all required environment variables are set and non-empty.

    Only runs in production — local mode uses defaults from Settings.
    """
    import os

    if os.environ.get("ENVIRONMENT", "local") == "local":
        return

    required = {
        "GCP_GCS_BUCKET_NAME": "GCS bucket for data lake storage",
        "GCP_PROJECT_ID": "GCP project ID",
    }

    missing = []
    for var, desc in required.items():
        val = os.environ.get(var, "").strip()
        if not val:
            missing.append(f"  {var} — {desc}")

    if "GCP_GCS_BUCKET_NAME" in [m.split(" —")[0].strip() for m in missing]:
        alt = os.environ.get("GCS_BUCKET_NAME", "")
        if alt:
            missing = [
                (
                    m + f" (found GCS_BUCKET_NAME={alt!r}"
                    " — rename to GCP_GCS_BUCKET_NAME)"
                    if "GCP_GCS_BUCKET_NAME" in m
                    else m
                )
                for m in missing
            ]

    if missing:
        msg = "Missing required environment variables:\n" + "\n".join(missing)
        logger.error(msg)
        raise SystemExit(1)


_CHECKPOINT_PATH = Path("/tmp/mapear_rss_checkpoint.json")
_GCS_CHECKPOINT_PREFIX = "backfill_checkpoints/rss"


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc


def _is_section_feed(feed_url: str) -> bool:
    """Return True when the feed URL is a deep-path section of a larger site.

    Section feeds (e.g. ``g1.globo.com/rss/g1/rn/rio-grande-do-norte/``) have
    sitemaps that cover the entire domain, not just the monitored section.
    Sitemap discovery for these domains would flood the frontier with millions
    of irrelevant national articles. We rely on the RSS feed phase alone to
    capture their RN-relevant content.

    Heuristic: path depth ≥ 2 segments (after stripping leading/trailing
    slashes) indicates a section feed.
    """
    from urllib.parse import urlparse

    path = urlparse(feed_url).path.strip("/")
    return path.count("/") >= 2


def _gcs_checkpoint_blob(backfill_date: date) -> str:
    return f"{_GCS_CHECKPOINT_PREFIX}/{backfill_date.isoformat()}.json"


def _load_gcs_checkpoint(backfill_date: date) -> set[str]:
    """Load the set of already-discovered domains from GCS (prod) or /tmp (local).

    Persists across Cloud Run executions so successive backfill runs skip
    the sitemap-discovery phase for domains already added to the frontier.
    """
    from mapear_rss.config import get_rss_settings

    settings = get_rss_settings()
    if settings.is_local:
        local_path = Path(f"/tmp/mapear_rss_bkchk_{backfill_date.isoformat()}.json")
        if not local_path.exists():
            return set()
        try:
            return set(json.loads(local_path.read_text()).get("discovered_domains", []))
        except Exception:
            return set()
    try:
        from google.cloud import storage

        client = storage.Client(project=settings.gcp.project_id)
        bucket = client.bucket(settings.gcp.gcs_bucket_name)
        blob = bucket.blob(_gcs_checkpoint_blob(backfill_date))
        if not blob.exists():
            return set()
        data = json.loads(blob.download_as_text(encoding="utf-8"))
        domains: set[str] = set(data.get("discovered_domains", []))
        logger.info(
            "backfill_gcs_checkpoint_loaded: {n} already-discovered domains",
            n=len(domains),
        )
        return domains
    except Exception as exc:
        logger.warning(
            "backfill_gcs_checkpoint_load_failed: {err}"
            " — discovery will re-run for all domains",
            err=str(exc),
        )
        return set()


def _save_gcs_checkpoint(backfill_date: date, discovered_domains: set[str]) -> None:
    """Persist the set of sitemap-discovered domains so the next run can skip them."""
    from mapear_rss.config import get_rss_settings

    settings = get_rss_settings()
    payload = json.dumps(
        {"discovered_domains": sorted(discovered_domains)}, ensure_ascii=False
    )
    if settings.is_local:
        local_path = Path(f"/tmp/mapear_rss_bkchk_{backfill_date.isoformat()}.json")
        try:
            local_path.write_text(payload)
        except Exception as exc:
            logger.warning("backfill_local_checkpoint_save_failed: {err}", err=str(exc))
        return
    try:
        from google.cloud import storage

        client = storage.Client(project=settings.gcp.project_id)
        bucket = client.bucket(settings.gcp.gcs_bucket_name)
        blob = bucket.blob(_gcs_checkpoint_blob(backfill_date))
        blob.upload_from_string(payload, content_type="application/json")
        logger.info(
            "backfill_gcs_checkpoint_saved: {n} discovered domains → gs://{bucket}/{blob}",
            n=len(discovered_domains),
            bucket=settings.gcp.gcs_bucket_name,
            blob=_gcs_checkpoint_blob(backfill_date),
        )
    except Exception as exc:
        logger.warning("backfill_gcs_checkpoint_save_failed: {err}", err=str(exc))


def _load_checkpoint() -> set[str]:
    """Return set of already-processed URLs from a previous backfill run."""
    if not _CHECKPOINT_PATH.exists():
        return set()
    try:
        data = json.loads(_CHECKPOINT_PATH.read_text())
        return set(data.get("processed_urls", []))
    except Exception as exc:
        logger.warning(
            "checkpoint_load_failed: {path} is corrupt or unreadable ({err}) — "
            "backfill will re-process all URLs",
            path=_CHECKPOINT_PATH,
            err=str(exc),
        )
        return set()


def _save_checkpoint(processed_urls: set[str]) -> None:
    try:
        _CHECKPOINT_PATH.write_text(
            json.dumps({"processed_urls": list(processed_urls)}, ensure_ascii=False)
        )
    except Exception as e:
        logger.warning("checkpoint_save_failed: {err}", err=str(e))


def _discover_sitemap_urls(
    feed_urls: list[str],
    since: date,
    skip_domains: set[str] | None = None,
) -> tuple[list[dict], set[str]]:
    """Run sitemap extraction for all feed domains and return frontier-ready dicts.

    Domains in ``skip_domains`` are skipped entirely — their URLs are already
    in the PostgreSQL frontier from a previous run (recorded in the GCS checkpoint).

    Returns ``(urls, newly_discovered_domains)`` so the caller can persist
    the newly discovered domains to the GCS checkpoint.
    """
    from urllib.parse import urlparse

    from mapear_rss.discovery.sitemap_extractor import SitemapExtractor

    skip = skip_domains or set()
    extractor = SitemapExtractor()
    result: list[dict] = []
    seen: set[str] = set()
    newly_discovered: set[str] = set()
    skipped: set[str] = set()

    for feed_url in feed_urls:
        domain = urlparse(feed_url).netloc
        if not domain or domain in seen:
            continue
        seen.add(domain)
        if domain in skip:
            skipped.add(domain)
            continue
        try:
            articles = extractor.extract_from_domain(domain, since)
            for a in articles:
                result.append(
                    {
                        "url": a.url,
                        "source_feed": a.source_sitemap or feed_url,
                        "published_at": (
                            datetime(
                                a.lastmod.year,
                                a.lastmod.month,
                                a.lastmod.day,
                                tzinfo=UTC,
                            )
                            if a.lastmod
                            else None
                        ),
                    }
                )
            newly_discovered.add(domain)
            logger.info(
                "sitemap_backfill: {count} candidate URLs from {domain}",
                count=len([r for r in result]),
                domain=domain,
            )
        except Exception as e:
            logger.warning(
                "sitemap_backfill: extraction failed for {domain}: {err}",
                domain=domain,
                err=str(e),
            )

    logger.info(
        "sitemap_backfill: {count} candidate URLs from {n_new} domains "
        "({n_skip} domains skipped — already in frontier)",
        count=len(result),
        n_new=len(newly_discovered),
        n_skip=len(skipped),
    )
    return result, newly_discovered


def run_pipeline(
    backfill_since: datetime | None = None,
    backfill_start_date: date | None = None,
    batch_size: int = 10,
    checkpoint_interval: int = 50,
) -> None:
    """Execute the full RSS pipeline locally."""
    setup_logging()
    start_metrics_server()
    setup_tracing(service_name="mapear-rss")
    _validate_required_env_vars()
    settings = get_rss_settings()
    region = load_region(settings.mapear_region)
    narrative_explainer = _build_narrative_explainer(settings)
    # Stage 1E v2 — shadow A/B classifier. None when MAPEAR_SHADOW_RULE_VERSION_YAML
    # is unset (CI / default). A misconfigured YAML raises here, before the run.
    shadow_scorer = build_shadow_scorer(
        yaml_path=settings.shadow.rule_version_yaml,
        enabled=settings.shadow.enabled,
        region=region.id,
        tenant_id=settings.mapear_tenant_id,
        pipeline_version=PIPELINE_VERSION,
        source_type="rss",
    )
    if shadow_scorer is not None:
        logger.info(
            "Shadow scoring enabled (Stage 1E v2) — candidate rule_version={rv}",
            rv=shadow_scorer.shadow_rule_version,
        )

    logger.info(
        "Starting RSS pipeline (environment={env}, region={region})",
        env=settings.environment.value,
        region=region.id,
    )

    # --- Watermark setup ---
    run_started_at = datetime.now(UTC)

    # Normalise backfill_start_date (date-only) → backfill_since (datetime)
    if backfill_start_date is not None and backfill_since is None:
        backfill_since = datetime(
            backfill_start_date.year,
            backfill_start_date.month,
            backfill_start_date.day,
            tzinfo=UTC,
        )

    is_backfill = backfill_since is not None
    watermark_manager = WatermarkManager("rss")

    if is_backfill:
        min_published_at: datetime | None = backfill_since
        logger.info(
            "Backfill mode: ingesting from {since} (watermark will NOT be updated)",
            since=backfill_since.isoformat(),
        )
    else:
        min_published_at = watermark_manager.get_watermark()
        if min_published_at is None:
            min_published_at = run_started_at - timedelta(
                hours=_FIRST_RUN_LOOKBACK_HOURS
            )
            logger.info(
                "No watermark found — using {hours}h lookback ({ts})",
                hours=_FIRST_RUN_LOOKBACK_HOURS,
                ts=min_published_at.isoformat(),
            )

    batch_id = run_started_at.strftime("%Y%m%d_%H%M%S")
    # Lineage: ingestion_run_id é único por execução; propagado para silver+gold
    # para permitir trace de qual run produziu cada linha (TDT-RSS-LINEAGE).
    # `actor_run_id` permanece NULL — RSS lê feeds diretamente, sem actor Apify.
    # `rule_version` permanece NULL — sem versionamento explícito de heurísticas
    # NER/relevância no RSS (decisão deferida; ver tech_debt_rss_lineage.md).
    ingestion_run_id = f"rss-{uuid.uuid4().hex[:12]}"
    writer = get_storage_writer()
    warehouse = get_warehouse_loader() if not settings.is_local else None
    iceberg_writer = get_iceberg_writer()
    pubsub_publisher = get_pubsub_publisher()
    failed_loads: list[str] = []

    # Electoral scope gate — PersonResolver is the single source of truth
    # for who counts as a monitored target. Silver rows that resolve
    # OUT_OF_SCOPE still persist for auditing but will not reach Gold v2.
    for candidate in (
        Path("dbt/seeds/rn_targets.csv"),
        Path("../dbt/seeds/rn_targets.csv"),
    ):
        if candidate.exists():
            set_targets_seed_path(candidate)
            break
    person_resolver = PersonResolver(region=region)

    # --- Stage 1: Discovery ---
    logger.info("=== Stage 1: Discovery ===")
    from sqlalchemy import create_engine, text

    engine = create_engine(
        settings.postgres.dsn,
        pool_size=settings.postgres.pool_size,
        max_overflow=settings.postgres.max_overflow,
    )

    with engine.connect() as conn:
        # Verify required tables exist
        table_check = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('feed_sources', 'url_frontier')"
            )
        ).fetchall()
        existing = {row.table_name for row in table_check}
        missing = {"feed_sources", "url_frontier"} - existing
        if missing:
            logger.error(
                "Required tables missing: {tables}. "
                "Run 'psql -f scripts/init_db.sql' to initialize the database.",
                tables=missing,
            )
            raise RuntimeError(f"Required Postgres tables missing: {missing}")

        rows = conn.execute(
            text("SELECT url, is_rn_focused FROM feed_sources WHERE is_active = TRUE")
        ).fetchall()

    feed_rows = rows
    feed_urls = [row.url for row in feed_rows]
    rn_feed_urls = {row.url for row in feed_rows if row.is_rn_focused}
    feed_name_by_url = {}
    with engine.connect() as conn:
        name_rows = conn.execute(
            text("SELECT url, name FROM feed_sources WHERE is_active = TRUE")
        ).fetchall()
    feed_name_by_url = {row.url: row.name for row in name_rows}

    # --- One-time migration: deactivate UOL feed and its frontier URLs ---
    _deactivate_uol_feed(engine)

    if not feed_urls:
        logger.error("No active feeds found. Run 'make seed-feeds' to add feeds.")
        raise RuntimeError("No active feeds in feed_sources — run 'make seed-feeds'")

    # --- Feed health pre-check ---
    health_monitor = FeedHealthMonitor(
        engine=engine,
        consecutive_failure_threshold=settings.feed_health.consecutive_failure_threshold,
        timeout_s=settings.feed_health.timeout_s,
    )
    feed_health_report: dict = {}
    if settings.feed_health.enabled:
        named_feeds = [(feed_name_by_url.get(url, url), url) for url in feed_urls]
        health_result = health_monitor.check_all(named_feeds)
        daily_volumes = health_monitor.get_daily_volumes()
        feed_health_report = health_monitor.build_report(health_result, daily_volumes)
        logger.info(
            "feed_health_check — available={av}/{total}, unhealthy={uh}",
            av=health_result.available_feeds,
            total=health_result.total_feeds,
            uh=health_result.unhealthy,
        )

    reader = RSSReader()
    discovered = reader.fetch_multiple(
        feed_urls,
        rn_focused_feeds=rn_feed_urls,
        min_published_at=min_published_at,
    )

    frontier = URLFrontier(engine=engine)
    frontier.reset_stale_in_progress(timeout_minutes=30)
    frontier.purge_old_pending(ttl_days=14)
    frontier.add_urls(discovered)

    # --- Backfill: sitemap-based discovery + GCS checkpoint resume ---
    checkpoint_processed: set[str] = set()
    if is_backfill and backfill_since is not None:
        checkpoint_processed = _load_checkpoint()
        if checkpoint_processed:
            logger.info(
                "Backfill resuming from in-run checkpoint: {n} URLs already processed",
                n=len(checkpoint_processed),
            )

        # Load GCS checkpoint: domains already sitemap-discovered in previous runs.
        # This avoids spending ~18 min re-crawling sitemaps that are already fully
        # queued in the PostgreSQL frontier.
        already_discovered = _load_gcs_checkpoint(backfill_since.date())

        # Sitemap discovery is restricted to RN-focused feeds whose feed URL
        # points to the site root or a shallow path (≤1 segment).
        # Section feeds — e.g. g1.globo.com/rss/g1/rn/rio-grande-do-norte/ —
        # have sitemaps that cover the whole domain (313K articles for g1 alone)
        # while only ~0.3% are RN-relevant. The RSS feed phase already captures
        # those RN articles. Excluding them saves hundreds of unnecessary runs.
        _section_skipped: list[str] = []
        sitemap_source_urls = []
        for u in list(rn_feed_urls) if rn_feed_urls else feed_urls:
            if _is_section_feed(u):
                _section_skipped.append(_domain_of(u))
            else:
                sitemap_source_urls.append(u)
        if _section_skipped:
            logger.info(
                "sitemap_discovery: skipping {n} section-feed domains "
                "(sitemap covers whole site, not just RN section): {domains}",
                n=len(_section_skipped),
                domains=_section_skipped,
            )
        pending_discovery = [
            u for u in sitemap_source_urls if _domain_of(u) not in already_discovered
        ]

        if pending_discovery:
            logger.info(
                "sitemap_discovery: {n_pending} domains to discover "
                "({n_skip} already in GCS checkpoint)",
                n_pending=len({_domain_of(u) for u in pending_discovery}),
                n_skip=len(already_discovered),
            )
            sitemap_urls, newly_discovered = _discover_sitemap_urls(
                pending_discovery,
                backfill_since.date(),
                skip_domains=already_discovered,
            )
            if newly_discovered:
                already_discovered.update(newly_discovered)
                _save_gcs_checkpoint(backfill_since.date(), already_discovered)
        else:
            logger.info(
                "sitemap_discovery_skipped: all {n} RN domains already in GCS"
                " checkpoint — proceeding directly to scraping",
                n=len(already_discovered),
            )
            sitemap_urls = []

        # Filter out already-processed URLs from in-run checkpoint
        new_sitemap = [u for u in sitemap_urls if u["url"] not in checkpoint_processed]
        if new_sitemap:
            from mapear_domain.models.base import DiscoveredURL

            discovered_sitemap = [
                DiscoveredURL(
                    url=u["url"],
                    source_feed=u["source_feed"],
                    published_at=u.get("published_at"),
                )
                for u in new_sitemap
            ]
            added = frontier.add_urls(discovered_sitemap)
            logger.info(
                "Backfill: added {added}/{total} sitemap URLs to frontier",
                added=added,
                total=len(new_sitemap),
            )

        # Reset in-memory cooldown so blocked domains get a fresh attempt
        # (DomainCooldown is instantiated later inside Scraper, so we expose
        # reset via FORCE_SCRAPE env or call reset() post-construction below)

    # --- Stage 2: Extraction ---
    logger.info("=== Stage 2: Extraction ===")
    # Backfill: pull 200 URLs per worker (10× normal) to maximize throughput
    # within the task-timeout window. Normal incremental runs keep 20× to
    # avoid over-committing the frontier.
    pending_limit = settings.scraper.max_workers * (200 if is_backfill else 20)
    pending = frontier.get_pending(limit=pending_limit)
    initial_pending_empty = not pending
    starvation_recovered = 0

    # Frontier starvation recovery: discovery returned URLs but none of
    # them were new (all already completed/failed). Without this, the
    # run would sit idle and produce zero throughput until something
    # new shows up in the feeds.
    if initial_pending_empty and len(discovered) > 0:
        if settings.scraper.frontier_enable_recirculation:
            starvation_recovered = frontier.recirculate_stale(
                ttl_hours=settings.scraper.frontier_reprocess_ttl_hours,
                limit=settings.scraper.frontier_recirculation_limit,
                include_failed=settings.scraper.frontier_recirculate_include_failed,
            )
            logger.warning(
                "starvation_recovery {payload}",
                payload=json.dumps(
                    {
                        "reason": "pending_empty_with_discovered",
                        "discovered": len(discovered),
                        "recirculated": starvation_recovered,
                        "ttl_hours": settings.scraper.frontier_reprocess_ttl_hours,
                    }
                ),
            )
            if starvation_recovered > 0:
                pending = frontier.get_pending(limit=pending_limit)
        else:
            logger.warning(
                "starvation_recovery disabled — run will be idle "
                "(discovered={d}, pending=0)",
                d=len(discovered),
            )

    scraper = Scraper()
    if is_backfill:
        # Release any in-memory cooldown state so blocked domains get retried
        released = scraper.cooldown.reset(force=True)
        if released > 0:
            logger.info(
                "Backfill: released {n} domains from cooldown",
                n=released,
            )

    counters = FetchCounters()
    unique: list[RawArticle] = []
    articles: list[RawArticle] = []
    rn_count = 0
    diversity_report: dict = {}
    pipeline_succeeded = False
    try:
        if not pending:
            logger.info("No pending URLs to extract.")
            pipeline_succeeded = True
            return

        counters.fetched_main = len(pending)
        try:
            articles = scraper.scrape_batch(pending)
        except Exception:
            raise
        counters.extracted_main = len(articles)

        # Load cross-batch hashes and canonical URLs BEFORE marking completed
        dedup = Deduplicator(engine=engine)
        dedup.load_existing_hashes()
        dedup.load_existing_canonical_urls()

        # Batch update frontier status. URLs deferred by domain cooldown
        # must stay ``pending`` so the next run can retry them — otherwise
        # a single bad window burns the retry budget for the whole domain.
        article_by_url = {str(a.url): a for a in articles}
        completed = [
            (item["url"], article_by_url[item["url"]].content_hash)
            for item in pending
            if item["url"] in article_by_url
        ]
        failed = [
            item["url"]
            for item in pending
            if item["url"] not in article_by_url
            and item["url"] not in scraper.deferred_urls
        ]
        frontier.mark_completed_batch(completed)
        frontier.mark_failed_batch(failed)
        deferred = [
            item["url"]
            for item in pending
            if item["url"] not in article_by_url
            and item["url"] in scraper.deferred_urls
        ]
        frontier.mark_deferred_batch(deferred)

        # Checkpoint: record processed URLs during backfill so restarts can resume
        if is_backfill:
            processed_batch = {item["url"] for item in pending}
            checkpoint_processed.update(processed_batch)
            if len(checkpoint_processed) % checkpoint_interval < len(processed_batch):
                _save_checkpoint(checkpoint_processed)
                logger.debug(
                    "Backfill checkpoint saved: {n} URLs",
                    n=len(checkpoint_processed),
                )

        # --- Stage 2b: Retry failed URLs ---
        retryable = frontier.get_retryable(max_retries=3, limit=50)
        if retryable:
            logger.info(
                "Retrying {count} previously failed URLs",
                count=len(retryable),
            )
            counters.fetched_retry = len(retryable)
            retry_articles = scraper.scrape_batch(retryable)
            counters.extracted_retry = len(retry_articles)

            retry_by_url = {str(a.url): a for a in retry_articles}
            retry_ok = [
                (item["url"], retry_by_url[item["url"]].content_hash)
                for item in retryable
                if item["url"] in retry_by_url
            ]
            retry_fail = [
                item["url"]
                for item in retryable
                if item["url"] not in retry_by_url
                and item["url"] not in scraper.deferred_urls
            ]
            frontier.mark_completed_batch(retry_ok)
            frontier.mark_failed_batch(retry_fail)
            retry_deferred = [
                item["url"]
                for item in retryable
                if item["url"] not in retry_by_url
                and item["url"] in scraper.deferred_urls
            ]
            frontier.mark_deferred_batch(retry_deferred)
            articles.extend(retry_articles)

        counters.fetched_unique_urls = len(
            {item["url"] for item in pending}
            | {item["url"] for item in (retryable if retryable else [])}
        )

        if not articles:
            logger.warning("No articles extracted in this batch.")
            pipeline_succeeded = True
            return

        # Deduplicate across main + retry batches before quality gate
        seen_hashes: set[str] = set()
        unique_articles: list[RawArticle] = []
        for a in articles:
            if a.content_hash not in seen_hashes:
                seen_hashes.add(a.content_hash)
                unique_articles.append(a)
        if len(unique_articles) < len(articles):
            logger.info(
                "Removed {n} intra-batch duplicates",
                n=len(articles) - len(unique_articles),
            )
        articles = unique_articles

        # --- Diversity scoring ---
        diversity_scorer = DiversityScorer(
            threshold=settings.diversity.concentration_threshold
        )
        div_result = diversity_scorer.compute(articles)
        diversity_report = diversity_scorer.to_dict(div_result)

        # Save raw
        df_raw = records_to_dataframe(articles)
        df_raw = _stamp_tenant_id(df_raw, settings.mapear_tenant_id)
        if not validate_raw(df_raw):
            logger.error("Raw quality gate FAILED — skipping write")
            quality_gate_failures.labels(layer="raw").inc()
            return
        raw_uri = write_dataframe_as_parquet(
            writer, df_raw, RAW_ARTICLE_SCHEMA, "raw", f"batch={batch_id}"
        )
        _load_to_warehouse(
            warehouse,
            raw_uri,
            f"{settings.gcp.bq_dataset_raw}.raw_articles",
            failed_loads,
            merge_key="content_hash",
        )

        # --- Stage 2.5: Pub/Sub publish (Eixo 1 v2 — streaming path) ---
        # Fire-and-forget: failures are logged but never block the batch.
        # The streaming consumer (Cloud Run Service) picks these up and
        # writes inline NER+sentiment to Iceberg within ~1-2 minutes.
        # Enabled only when MAPEAR_PUBSUB_ENABLED=true (default: false).
        _n_published = pubsub_publisher.publish_batch(articles)
        if _n_published:
            logger.info(
                "Stage 2.5: {n} raw articles published to Pub/Sub",
                n=_n_published,
            )

        # --- Stage 3: Silver Transform ---
        logger.info("=== Stage 3: Silver Transform ===")
        unique = dedup.deduplicate(articles)

        ner = NERExtractor(region=region)
        silver = ner.extract_batch(unique, rn_feed_urls=rn_feed_urls)

        # Electoral scope resolution — assigns person_id, scope_status,
        # resolution_confidence so dbt Gold v2 can filter IN_SCOPE only.
        # OUT_OF_SCOPE rows still land in silver for auditing and NER drift.
        # Lineage: ingestion_run_id + pipeline_version são populados aqui
        # (TDT-RSS-LINEAGE) para que cada linha silver+gold rastreie até o run.
        for article in silver:
            res = person_resolver.resolve_best(
                mentions=article.mentioned_persons,
                context=f"{article.title}. {article.content_clean}",
                platform="rss",
            )
            article.person_id = res.person_id
            article.scope_status = res.scope_status.value
            article.resolution_confidence = res.confidence
            article.ingestion_run_id = ingestion_run_id
            article.pipeline_version = PIPELINE_VERSION

        if silver:
            df_silver = records_to_dataframe(silver)
            df_silver = _stamp_tenant_id(df_silver, settings.mapear_tenant_id)
            if not validate_silver(df_silver):
                logger.error("Silver quality gate FAILED — skipping write")
                quality_gate_failures.labels(layer="silver").inc()
                return
            silver_uri = write_dataframe_as_parquet(
                writer,
                df_silver,
                SILVER_ARTICLE_SCHEMA,
                "silver",
                f"batch={batch_id}",
            )
            _load_to_warehouse(
                warehouse,
                silver_uri,
                f"{settings.gcp.bq_dataset_silver}.silver_articles",
                failed_loads,
                merge_key="content_hash",
            )

            # Stage 3.7: Iceberg write (Eixo 1 v1 — opt-in via MAPEAR_ICEBERG_ENABLED).
            _write_silver_to_iceberg(df_silver, iceberg_writer, batch_id)

        rn_articles = [a for a in silver if a.is_rn_relevant]
        rn_count = len(rn_articles)

        # --- Stage 4: Gold Enrichment ---
        logger.info("=== Stage 4: Gold Enrichment ===")

        if rn_articles:
            sentiment_analyzer = SentimentAnalyzer()
            topic_modeler = TopicModeler()
            trend_scorer = TrendScorer()
            entities = list(
                region.get_city_names()
                | region.get_mayor_names()
                | region.get_governor_names()
            )
            trend_scores = trend_scorer.score_batch(entities, rn_articles)

            cache = ContentCache.build()

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

            gold_articles = []
            for article, sent, topic in zip(
                rn_articles, sentiments, topics, strict=False
            ):
                trend = max(
                    (trend_scores.get(c, 0) for c in article.mentioned_cities),
                    default=0.0,
                )
                gold = _silver_to_gold(
                    article=article,
                    sent=sent,
                    topic=topic,
                    trend_score=trend,
                )
                gold_articles.append(gold)

            # Stage 4.5: Political sentiment classification (C3.1 / BL-F2-05).
            # Stage 4.5b: shadow A/B rows when shadow_scorer is enabled.
            shadow_rows = _classify_political_sentiment(gold_articles, shadow_scorer)

            # Stage 4.6: LLM narrative summary on ALERT rows (Eixo 2 v1) +
            # per-call audit log (Eixo 6 light).
            silver_index = {s.content_hash: s for s in silver}
            _apply_narrative_explainer(
                gold_articles,
                silver_index,
                narrative_explainer,
                tenant_id=settings.mapear_tenant_id,
                region_id=region.id,
                provider=settings.llm.provider,
                model=settings.llm.model,
                coverage=settings.llm.explainer_coverage,
            )

            if gold_articles:
                null_source_count = sum(
                    1 for g in gold_articles if g.topic_id_source is None
                )
                if null_source_count > 0:
                    logger.error(
                        "topic_id_source IS NULL for {n} gold articles "
                        "in batch {batch} — TDT-TOPIC-01 sentinel CRIT",
                        n=null_source_count,
                        batch=batch_id,
                    )
                df_gold = records_to_dataframe(gold_articles)
                df_gold = _stamp_tenant_id(df_gold, settings.mapear_tenant_id)
                if not validate_gold(df_gold):
                    logger.error("Gold quality gate FAILED — skipping write")
                else:
                    gold_uri = write_dataframe_as_parquet(
                        writer,
                        df_gold,
                        GOLD_ARTICLE_SCHEMA,
                        "gold",
                        f"batch={batch_id}",
                    )
                    _load_to_warehouse(
                        warehouse,
                        gold_uri,
                        f"{settings.gcp.bq_dataset_gold}.gold_articles",
                        failed_loads,
                        merge_key="content_hash",
                    )

            # Stage 4.5b: persist shadow A/B rows (Stage 1E v2). No-op when
            # shadow_scorer is None — shadow_rows is then an empty list.
            _write_shadow_rows(
                shadow_rows,
                writer,
                warehouse,
                bq_dataset_silver=settings.gcp.bq_dataset_silver,
                batch_id=batch_id,
                failed_loads=failed_loads,
            )

            logger.info(
                "Gold enrichment: {count} articles enriched",
                count=len(gold_articles),
            )

            # Generate quality report for Gold layer
            if gold_articles:
                report = generate_quality_report(df_gold, "gold", source_type="rss")
                if report.get("critical_failure"):
                    logger.error(
                        "Gold quality report has critical failures — "
                        "review null rates for critical fields"
                    )

        # --- Summary ---
        pipeline_succeeded = True
        stats = frontier.get_stats()
        for status, count in stats.items():
            frontier_queue_depth.labels(status=status).set(count)

        logger.info(
            "Pipeline complete — "
            "discovered={discovered}, extracted={extracted}, "
            "unique={unique}, rn_relevant={rn}, "
            "frontier_stats={stats}",
            discovered=len(discovered),
            extracted=len(articles),
            unique=len(unique),
            rn=rn_count,
            stats=stats,
        )
    finally:
        _emit_final_reports(
            scraper=scraper,
            frontier=frontier,
            counters=counters,
            discovered_count=len(discovered),
            unique_count=len(unique),
            rn_count=rn_count,
            initial_pending_empty=initial_pending_empty,
            starvation_recovered=starvation_recovered,
            diversity_report=diversity_report,
            feed_health_report=feed_health_report,
        )
        scraper.close()
        if pipeline_succeeded and not is_backfill and not failed_loads:
            watermark_manager.save_watermark(run_started_at)

    if failed_loads:
        logger.error(
            "Pipeline finished with {n} BQ load failures: {tables}",
            n=len(failed_loads),
            tables=failed_loads,
        )
        sys.exit(2)


def _emit_final_reports(
    *,
    scraper: Scraper,
    frontier: URLFrontier,
    counters: FetchCounters,
    discovered_count: int,
    unique_count: int,
    rn_count: int,
    initial_pending_empty: bool,
    starvation_recovered: int,
    diversity_report: dict | None = None,
    feed_health_report: dict | None = None,
) -> None:
    """Always emit run_report, starvation_report (if applicable), go_nogo_report.

    Each block is wrapped in its own try/except so a failure in one
    does not suppress the others. All three go to stdout as structured
    JSON for BigQuery log-based metrics.
    """
    report: dict | None = None
    try:
        counters.browser_attempts = int(scraper.browser_counts.get("attempts", 0))
        counters.browser_success = int(scraper.browser_counts.get("success", 0))
        counters.browser_failed = int(scraper.browser_counts.get("failed", 0))
        report = scraper.diagnostics.build_report(
            discovered=discovered_count,
            counters=counters,
            unique=unique_count,
            rn_relevant=rn_count,
            cooldown_skips=scraper.cooldown.total_skips(),
            cooldown_applied_count=scraper.cooldown.applied_count(),
            cooldown_reason_distribution=scraper.cooldown.reason_distribution(),
            deferred_by_cooldown=len(scraper.deferred_urls),
            diversity=diversity_report or {},
            feed_health=feed_health_report or {},
        )
        scraper.diagnostics.log_report(report)
    except Exception as e:
        logger.warning("Failed to build run report: {error}", error=str(e))

    if initial_pending_empty:
        try:
            stats = frontier.get_stats()
            starvation = {
                "reason": "initial_pending_empty",
                "discovered": discovered_count,
                "frontier_stats": stats,
                "recirculated": starvation_recovered,
                "final_fetched": counters.fetched_total,
            }
            logger.info(
                "starvation_report {payload}",
                payload=json.dumps(starvation, default=str),
            )
        except Exception as e:
            logger.warning("Failed to emit starvation_report: {error}", error=str(e))

    try:
        decision = _decide_go_nogo(report, counters)
        logger.info(
            "go_nogo_report {payload}",
            payload=json.dumps(decision, default=str),
        )
    except Exception as e:
        logger.warning("Failed to emit go_nogo_report: {error}", error=str(e))


def _decide_go_nogo(
    report: dict | None,
    counters: FetchCounters,
) -> dict:
    """Return a small ``{status, reasons}`` dict used as the run verdict.

    Kept intentionally simple — downstream alerting relies on stable
    JSON shape, so add reasons rather than reshuffling keys.
    """
    reasons: list[str] = []
    if report is None:
        return {"status": "nogo", "reasons": ["run_report_missing"]}

    if report.get("integrity_warning"):
        reasons.append("integrity_warning")

    fetched_total = counters.fetched_total
    if fetched_total > 20 and report.get("extraction_success_rate", 0.0) < 0.3:
        reasons.append("low_extraction_success_rate")

    applied = int(report.get("cooldown_applied_count", 0) or 0)
    if fetched_total > 0 and applied / fetched_total > 0.5:
        reasons.append("excessive_cooldown")

    return {
        "status": "nogo" if reasons else "go",
        "reasons": reasons,
        "fetched_total": fetched_total,
        "extraction_success_rate": report.get("extraction_success_rate"),
        "cooldown_applied_count": report.get("cooldown_applied_count"),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mapear_rss",
        description=(
            "Mapear-RSS pipeline — RSS news extraction"
            " for RN sociopolitical monitoring."
        ),
    )
    parser.add_argument(
        "--backfill-since",
        dest="backfill_since",
        default=None,
        metavar="ISO_TIMESTAMP",
        help=(
            "Ingest articles published after this ISO timestamp. "
            "Watermark is NOT updated after a backfill. "
            "Example: --backfill-since=2026-04-01T00:00:00Z"
        ),
    )
    parser.add_argument(
        "--backfill-start-date",
        dest="backfill_start_date",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Date-only shorthand for --backfill-since. Also triggers sitemap-based "
            "discovery for all configured feed domains. "
            "Example: --backfill-start-date=2025-01-01"
        ),
    )
    parser.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        default=10,
        metavar="INT",
        help="Number of URLs per extraction batch in backfill mode (default: 10).",
    )
    parser.add_argument(
        "--checkpoint-interval",
        dest="checkpoint_interval",
        type=int,
        default=50,
        metavar="INT",
        help=(
            "Save checkpoint every N processed URLs in backfill mode (default: 50). "
            "Checkpoint is written to /tmp/mapear_rss_checkpoint.json."
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    """Entry point."""
    args = _parse_args()
    backfill_since: datetime | None = None
    backfill_start_date: date | None = None

    if args.backfill_since:
        backfill_since = datetime.fromisoformat(
            args.backfill_since.replace("Z", "+00:00")
        )
    if args.backfill_start_date:
        backfill_start_date = date.fromisoformat(args.backfill_start_date)

    try:
        run_pipeline(
            backfill_since=backfill_since,
            backfill_start_date=backfill_start_date,
            batch_size=args.batch_size,
            checkpoint_interval=args.checkpoint_interval,
        )
    except Exception:
        logger.exception("Pipeline failed with unhandled exception")
        sys.exit(1)


if __name__ == "__main__":
    main()
