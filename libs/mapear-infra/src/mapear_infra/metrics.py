"""Prometheus metrics for pipeline observability.

Exposes counters, histograms, and gauges for key pipeline stages.
Metrics are no-ops if prometheus_client is not installed.
"""

from collections.abc import Generator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from loguru import logger

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False


def _noop_metric(*args: Any, **kwargs: Any) -> Any:
    """No-op metric for when prometheus_client is not installed."""

    class _Noop:
        def inc(self, *a: Any, **kw: Any) -> None:
            pass

        def dec(self, *a: Any, **kw: Any) -> None:
            pass

        def set(self, *a: Any, **kw: Any) -> None:
            pass

        def observe(self, *a: Any, **kw: Any) -> None:
            pass

        def labels(self, *a: Any, **kw: Any) -> "_Noop":
            return self

    return _Noop()


# --- Extraction ---
articles_extracted = (
    Counter(
        "mapear_articles_extracted_total",
        "Total articles extracted",
        ["source_type", "domain", "status"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

extraction_duration = (
    Histogram(
        "mapear_extraction_duration_seconds",
        "Time to extract a single article",
        buckets=[0.5, 1, 2, 5, 10, 30],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

# --- NER ---
ner_duration = (
    Histogram(
        "mapear_ner_batch_duration_seconds",
        "Time to process NER on a batch",
        buckets=[1, 5, 10, 30, 60, 120],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

ner_content_relevant = (
    Counter(
        "mapear_ner_rn_relevant_total",
        "Articles flagged as content-relevant",
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

# --- Sentiment / Enrichment ---
enrichment_duration = (
    Histogram(
        "mapear_enrichment_duration_seconds",
        "Time for gold enrichment batch",
        buckets=[5, 10, 30, 60, 120, 300],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

cache_hits = (
    Counter(
        "mapear_cache_hits_total",
        "Content cache hits",
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

cache_misses = (
    Counter(
        "mapear_cache_misses_total",
        "Content cache misses",
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

# --- Frontier ---
frontier_queue_depth = (
    Gauge(
        "mapear_frontier_queue_depth",
        "Number of URLs in each status",
        ["status"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

# --- Circuit Breaker ---
circuit_breaker_state = (
    Gauge(
        "mapear_circuit_breaker_open",
        "Whether circuit breaker is open for a domain",
        ["domain"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

# --- Pipeline ---
pipeline_batch_duration = (
    Histogram(
        "mapear_pipeline_batch_duration_seconds",
        "Total pipeline batch duration",
        buckets=[30, 60, 120, 300, 600, 1800],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

quality_gate_failures = (
    Counter(
        "mapear_quality_gate_failures_total",
        "Quality gate failures by layer",
        ["layer"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

bq_load_failures = (
    Counter(
        "mapear_bq_load_failures_total",
        "BigQuery load failures by target table",
        ["target_table"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

# --- Entity fill rate (social silver) ---
# Gauge atualizado a cada execução de pipeline. Label: platform.
entity_fill_cities = (
    Gauge(
        "mapear_social_entity_fill_cities_pct",
        "Percentual de posts silver com mentioned_cities não vazio",
        ["platform"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

entity_fill_politicians = (
    Gauge(
        "mapear_social_entity_fill_politicians_pct",
        "Percentual de posts silver com entidade política não vazia",
        ["platform", "role"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

ner_person_noise_rate = (
    Gauge(
        "mapear_ner_person_noise_rate_pct",
        "Percentual de entidades PERSON removidas pelo stoplist",
        ["platform"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

# % de posts de prefeito (person_id começa com "mayor_") com
# author_base_city preenchido.
# Esperado ~100%: qualquer desvio indica inconsistência person_id ↔ rn_entities.yml.
mayor_author_base_city_fill_pct = (
    Gauge(
        "mapear_social_mayor_author_base_city_fill_pct",
        "Percentual de posts de prefeito com author_base_city preenchido",
        ["platform"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)


# --- Language detection (social silver) ---
language_fill_pct = (
    Gauge(
        "mapear_social_language_fill_pct",
        "Percentual de posts silver com language preenchido após detecção",
        ["platform"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

language_detected_total = (
    Counter(
        "mapear_social_language_detected_total",
        "Total de posts por idioma detectado (acumulado por batch)",
        ["platform", "language"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)


# --- Social pipeline operational metrics (per run, per platform) ---
social_scraped_total = (
    Counter(
        "mapear_social_scraped_total",
        "Raw posts fetched from platform API",
        ["platform"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

social_filtered_total = (
    Counter(
        "mapear_social_filtered_total",
        "Posts removed by temporal cutoff or intra-batch dedup",
        ["platform"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

social_stored_total = (
    Counter(
        "mapear_social_stored_total",
        "Posts written to Silver layer (IN_SCOPE only)",
        ["platform"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

social_errors_total = (
    Counter(
        "mapear_social_errors_total",
        "Pipeline errors by type (parse_error, schema_drift, api_error)",
        ["platform", "error_type"],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)

social_pipeline_latency = (
    Histogram(
        "mapear_social_pipeline_latency_seconds",
        "End-to-end latency per platform pipeline run (p50/p95 via quantile buckets)",
        ["platform"],
        buckets=[10, 30, 60, 120, 300, 600, 900, 1200, 1800],
    )
    if _HAS_PROMETHEUS
    else _noop_metric()
)


@contextmanager
def track_duration(metric: Any) -> Generator[None, None, None]:
    """Context manager to observe duration on a Histogram."""
    start = perf_counter()
    try:
        yield
    finally:
        metric.observe(perf_counter() - start)


def start_metrics_server(port: int = 9090) -> None:
    """Start the Prometheus metrics HTTP server."""
    if not _HAS_PROMETHEUS:
        logger.warning("prometheus_client not installed, metrics server disabled")
        return
    start_http_server(port)
    logger.info("Prometheus metrics server started on port {port}", port=port)
