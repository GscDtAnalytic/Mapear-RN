"""Application configuration loaded from environment variables."""

from enum import Enum
from pathlib import Path

from loguru import logger
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Environment(str, Enum):
    LOCAL = "local"
    PRODUCTION = "production"


class EnrichmentMode(str, Enum):
    LOCAL = "local"
    API = "api"
    SKIP = "skip"


class PostgresConfig(BaseSettings):
    host: str = "localhost"
    port: int = 5432
    db: str = "mapear_rn"
    user: str = "mapear"
    password: str = ""
    pool_size: int = 5
    max_overflow: int = 10

    model_config = {
        "env_prefix": "POSTGRES_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def dsn(self) -> str:
        return (
            f"postgresql+psycopg2://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.db}"
        )


class RedisConfig(BaseSettings):
    host: str = "localhost"
    port: int = 6379
    password: str = ""
    # Quando False, cache e circuit breaker viram no-op sem abrir conexão.
    # Útil em Cloud Run jobs enquanto o VPC connector/Memorystore está
    # instável (ver docs/ops/redis_rollout.md).
    enabled: bool = True
    # Memorystore com transit_encryption_mode=SERVER_AUTHENTICATION requer TLS.
    # Usar REDIS_SSL=true em produção.
    ssl: bool = False

    model_config = {
        "env_prefix": "REDIS_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def url(self) -> str:
        scheme = "rediss" if self.ssl else "redis"
        if self.password:
            return f"{scheme}://:{self.password}@{self.host}:{self.port}/0"
        return f"{scheme}://{self.host}:{self.port}/0"


class GCPConfig(BaseSettings):
    project_id: str = ""
    region: str = "southamerica-east1"
    gcs_bucket_name: str = ""
    bq_dataset_raw: str = "mapear_raw"
    bq_dataset_silver: str = "mapear_silver"
    bq_dataset_gold: str = "mapear_gold"

    model_config = {
        "env_prefix": "GCP_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class LLMConfig(BaseSettings):
    """LLM provider config — Eixo 2 v1 (LLM-as-explainer).

    Read by ``mapear_nlp.llm.client.get_llm_client``. v1 supports
    Anthropic (default), OpenAI, and Vertex AI via the same interface.
    The API key resolution order is: explicit ``api_key`` env var →
    Cloud Secret Manager secret (``api_key_secret``) → raise. The
    secret-manager fallback lets prod deployments avoid shipping the
    raw key in env vars.
    """

    # "anthropic" | "openai" | "vertex"
    provider: str = "anthropic"
    # Anthropic default targets Haiku 4.5 — cheap, fast, sufficient for
    # the explainer task. Override per-provider via MAPEAR_LLM_MODEL.
    model: str = "claude-haiku-4-5-20251001"
    # Cap on output tokens. Narrative summaries should fit in ~120
    # tokens; keep the cap small to cap cost on a bad day.
    max_tokens: int = 200
    # Inference temperature. Low + deterministic for an explainer task.
    temperature: float = 0.2
    # Per-call wall-clock timeout (seconds). Pipeline already retries
    # at the batch level via ALERT-only gating.
    timeout_seconds: float = 30.0
    # Raw API key — preferred for local dev. Empty in prod.
    api_key: str = ""
    # Secret Manager resource name — preferred for prod. Empty locally.
    # Resolved lazily by the client when api_key is empty.
    # e.g. ``projects/<num>/secrets/anthropic-api-key/versions/latest``
    api_key_secret: str = ""
    # Cache control — ALERT-only volume is ~5% of pipeline rows, so the
    # cache hit-rate dominates the API bill. Disabling is for tests.
    cache_enabled: bool = True
    # GCS prefix under settings.gcp.gcs_bucket_name where the
    # narrative-explainer cache writes its content-addressed blobs.
    cache_gcs_prefix: str = "narrative_cache/"
    # Eixo 6 light — PII redaction level applied to article content
    # before it leaves the warehouse for an external LLM provider.
    # One of: "none" | "masked" | "pseudonymized" | "dropped".
    # Default MASKED — emails / CPFs / phones become [email] / [cpf] /
    # [phone] tokens. PSEUDONYMIZED additionally requires
    # MAPEAR_LLM_PII_HMAC_KEY so tags stay stable across runs.
    # See docs/decisions/adr-eixo-6-light-pii-redaction.md.
    pii_level: str = "masked"
    # HMAC key for PSEUDONYMIZED redaction. Empty by default; the
    # redactor raises when the level is PSEUDONYMIZED and the key is
    # unset, so misconfiguration fails loud at pipeline start.
    pii_hmac_key: str = ""

    # --- Eixo 2 v2b — stance detection (out-of-band job) ---
    # Master switch. When False the stance job writes nothing and exits
    # cleanly — same pattern as MAPEAR_EMBEDDINGS_CLUSTER_ENABLED.
    stance_enabled: bool = True
    # GCS prefix for the stance cache, separate from the narrative cache
    # so rotating the stance prompt does not invalidate narrative entries.
    stance_cache_gcs_prefix: str = "narrative_stance/"

    # --- Eixo 2 v2d — mayor endorsement investigation (out-of-band job) ---
    # Master switch for the endorsement detector job. When False the job
    # writes nothing and exits cleanly.
    endorsement_enabled: bool = True
    # Model for the endorsement investigation. Defaults to Sonnet — this is
    # a low-volume, high-value analytical task (judging political alignment
    # across several articles), distinct from the high-volume Haiku explainer.
    endorsement_model: str = "claude-sonnet-4-6"
    # Output-token cap. The verdict carries a short rationale, so it needs
    # more room than the stance JSON but stays bounded.
    endorsement_max_tokens: int = 600
    # GCS prefix for the endorsement cache — separate so rotating this
    # prompt does not invalidate narrative or stance entries.
    endorsement_cache_gcs_prefix: str = "mayor_endorsement/"

    # --- Eixo 2 v1 — narrative explainer coverage ---
    # "alert" (default): the explainer only summarises ALERT / strongly-
    # negative articles — keeps the bill at ~5% of pipeline rows.
    # "all": summarise every article regardless of sentiment, so positive
    # and neutral narratives also get an LLM summary (~20x the cost).
    explainer_coverage: str = "alert"

    model_config = {
        "env_prefix": "MAPEAR_LLM_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class CIBConfig(BaseSettings):
    """Coordinated Inauthentic Behavior detection — Eixo 3 v1.

    Drives the author co-activation graph in ``mapear_nlp.graph.coactivation``
    and the silver_author_activations write path in the social pipeline.
    See docs/decisions/adr-eixo-3-v1-coactivation-graph.md.
    """

    # Sliding-window width for co-post detection. 24h is the prod default
    # — coordination campaigns typically synchronize within a news cycle.
    window_hours: float = 24.0
    # Minimum windowed co-post count for an author pair to appear in the
    # output. 3 is a conservative floor: two authors that fire together
    # only once or twice are too noisy to act on.
    min_overlap: int = 3
    # Master switch for the activation write path. Defaults to True
    # because the table is purely additive (lineage from
    # silver_social_posts); setting False skips the parquet write and
    # the BQ MERGE without touching the rest of the pipeline.
    enabled: bool = True

    # --- Eixo 3 v2 — community detection (out-of-band job) ---
    # Algorithm for ``mapear_nlp.graph.community.detect_communities``.
    # "louvain" (default): modularity-optimizing, recommended for
    # surfacing tight squads. "label_propagation": fast, deterministic,
    # fuzzier boundaries. Both ship in networkx.
    community_algorithm: str = "louvain"
    # Louvain resolution parameter. >1.0 → more, smaller communities.
    # <1.0 → fewer, larger. 1.0 is the standard modularity definition.
    community_resolution: float = 1.0
    # Seed for Louvain — deterministic output across runs. Set to None
    # only if running multiple seeds and aggregating (consensus
    # clustering); not implemented in v2a.
    community_seed: int = 42
    # Minimum members to emit a community. Communities with fewer
    # members are dropped on the floor — they are usually graph
    # artifacts (isolated dyads), not actionable coordination.
    community_min_size: int = 3

    # --- Eixo 3 v2b — cross-platform author identity resolution ---
    # Whether the co-activation graph should consume the persona
    # mapping when building AuthorKey. Default OFF: v1+v2a outputs
    # stay identical until the operator flips this. Trigger to enable
    # is ROI demonstrable on the persona job.
    use_personas: bool = False
    # Minimum Jaro-Winkler similarity on normalised handles to
    # trigger a MATCH (combined with display-name corroboration).
    # 0.90 is the calibrated default: realistic suffixes like
    # ``prefeito_x_oficial`` vs ``prefeito.x`` score ~0.91, and we
    # want them to merge when the display name also corroborates.
    # Homonyms with divergent display names land in AMBIGUOUS rather
    # than MATCH because ``er_display_name_similarity`` is also
    # required for the MATCH path.
    er_handle_similarity: float = 0.90
    # Minimum Jaro-Winkler similarity on normalised display names.
    # Required alongside handle similarity unless the content-hash
    # bridge kicks in (see below).
    er_display_name_similarity: float = 0.90
    # Number of shared content_hashes that triggers the cross-platform
    # bridge (a MATCH even without strong display-name corroboration).
    # 1 is the floor; downstream the engine still requires a minimum
    # handle similarity to avoid merging unrelated accounts on a
    # single viral post.
    er_min_shared_content: int = 1
    # Master switch for the content-hash bridge. Set False to require
    # handle + display-name evidence for every MATCH (more conservative
    # / lower recall).
    er_use_content_hash_bridge: bool = True
    # Master switch for the author-resolution audit log. Defaults to
    # True — each persona created emits a structured loguru line so
    # operators can audit cross-platform merges.
    er_audit_enabled: bool = True

    # --- Eixo 3 v3 — inauthenticity scoring ---
    # Synchrony weight in the composite inauthenticity score.
    # Represents how often the pair fires together (co_post_count).
    score_sync_weight: float = 0.4
    # Alignment weight — lifetime Jaccard over political targets.
    score_jaccard_weight: float = 0.4
    # Content similarity weight — mean cosine similarity of post embeddings.
    # Contributes only when content_embeddings are supplied to the job.
    score_content_sim_weight: float = 0.2
    # co_post_count value that saturates the synchrony component to 1.0.
    # Pairs that exceed this threshold are fully coordinated by this signal.
    score_sync_cap: float = 20.0

    # --- Eixo 3 v3 — cluster-identity persistence ---
    # Minimum Jaccard similarity between community member sets across
    # adjacent days to consider them the same cluster series.
    cluster_series_threshold: float = 0.5

    model_config = {
        "env_prefix": "MAPEAR_CIB_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class EmbeddingsConfig(BaseSettings):
    """Embedding + narrative clustering config — Eixo 2 v2a.

    Drives the embedding pipeline in ``mapear_nlp.embeddings`` and the
    out-of-band narrative-clustering job
    (``mapear_nlp.clustering.run_narrative_clustering``). See ADR
    docs/decisions/adr-eixo-2-v2a-narrative-clustering.md.
    """

    # Sentence-transformer model name. Default is the standard
    # multilingual paraphrase model — strong pt-BR coverage, 768d,
    # local inference (no API cost). Override per-region or per-tenant
    # via env when a larger multilingual model is needed.
    model: str = "paraphrase-multilingual-mpnet-base-v2"
    # Cache control — narrative_summary volume is ALERT-only (~5% of
    # rows), and an embedding takes ~50ms locally, so cache hit-rate
    # only matters at scale. Disabling is for tests / debugging.
    cache_enabled: bool = True
    # GCS prefix under settings.gcp.gcs_bucket_name for the
    # content-addressed embedding cache. Mirrors the LLM cache pattern.
    cache_gcs_prefix: str = "narrative_embeddings/"

    # --- Eixo 2 v2a — narrative clustering (out-of-band job) ---
    # Algorithm. "hdbscan" (default): density-based, no fixed k,
    # handles outliers naturally — the right tool when you don't know
    # how many narrative threads are active on a given day.
    # "cosine_threshold": connected-components on a cosine-similarity
    # graph; pure-Python fallback so the eval gate still runs when
    # hdbscan is not installed.
    cluster_algorithm: str = "hdbscan"
    # Minimum cluster size for HDBSCAN. Same floor as the community
    # detector (3) — a 2-narrative pair is a coincidence, not a thread.
    cluster_min_size: int = 3
    # Distance metric. "cosine" is the standard for sentence embeddings
    # (vectors are unit-normalised by the encoder so cosine == euclidean
    # on the hypersphere, but cosine is the convention).
    cluster_distance_metric: str = "cosine"
    # Cosine-similarity threshold for the cosine_threshold algorithm.
    # 0.75 is a calibrated default — narratives with similarity >= 0.75
    # share the same campaign / framing in practice. Tune per region.
    cluster_cosine_threshold: float = 0.75
    # Master switch for the clustering write path. Defaults to True;
    # setting False is a no-op killswitch without redeploying the job.
    cluster_enabled: bool = True

    # --- Eixo 2 v2a social — social post content embeddings (out-of-band job) ---
    # GCS prefix for the social post embedding cache — separate from the
    # narrative_embeddings/ prefix so rotating the model does not evict
    # previously cached social vectors.
    social_post_cache_gcs_prefix: str = "social_post_embeddings/"
    # Master switch for the social embedding write path. False → job exits 0
    # without writing, matching the pattern of cluster_enabled / stance_enabled.
    social_post_embedding_enabled: bool = True

    model_config = {
        "env_prefix": "MAPEAR_EMBEDDINGS_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class IcebergConfig(BaseSettings):
    """Lakehouse storage config — Eixo 1 v1 (Iceberg foundation).

    Drives the IcebergWriter in ``mapear_storage.loaders.iceberg_writer``
    and the opt-in silver write path in each ETL pipeline.
    See docs/decisions/adr-eixo-1-v1-iceberg-foundation.md.

    Catalog backend: SqlCatalog (PyIceberg).
    - Local: SQLite file at ``catalog_uri`` (auto-created).
    - Prod: Cloud SQL PostgreSQL DSN (reuses existing infra).

    Warehouse: GCS URI in prod (``gs://<bucket>/iceberg/``),
    local filesystem path in local dev.
    """

    # Master switch. False by default — existing BQ writes are unchanged
    # until operator explicitly enables and validates the Iceberg path.
    enabled: bool = False
    # GCS URI prefix for table data and metadata, e.g.
    # ``gs://mapear-rn-bucket/iceberg/``. In local dev use an absolute
    # path like ``/tmp/mapear_iceberg``.
    warehouse: str = ""
    # SQLAlchemy URI for the catalog metastore.
    # Empty → SQLite at ``<warehouse>/catalog.db`` (local-safe default).
    # Prod: ``postgresql+psycopg2://user:pw@host:5432/mapear_rn``
    catalog_uri: str = ""
    # Iceberg namespace that groups all Mapear tables.
    namespace: str = "mapear"
    # BigLake connection ID — the resource created by Terraform at
    # ``module.iceberg.biglake_connection_id`` (e.g. "mapear-iceberg").
    # Empty string disables the automatic refresh of BigLake external tables
    # after each Iceberg write (safe default for local dev and environments
    # that do not use BigLake).
    biglake_connection: str = ""
    # BigQuery dataset that hosts the BigLake external tables. Must match
    # the dataset where ``CREATE OR REPLACE EXTERNAL TABLE`` is executed.
    biglake_dataset: str = "mapear_silver"

    model_config = {
        "env_prefix": "MAPEAR_ICEBERG_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class PubSubConfig(BaseSettings):
    """Pub/Sub publisher config — Eixo 1 v2 (streaming consumer).

    Controls the fire-and-forget publication of RawArticle records to
    ``mapear-rss-raw`` from within the RSS batch pipeline (Stage 2.5).
    The streaming consumer (Cloud Run Service) subscribes and processes
    inline NER + sentiment, writing to Iceberg within ~1-2 minutes.

    See docs/decisions/adr-eixo-1-v2-streaming-consumer.md.
    """

    # Master switch. False by default — existing batch pipeline unchanged
    # until operator enables and the Cloud Run Service consumer is deployed.
    enabled: bool = False
    # Pub/Sub topic name (short form; project is taken from GCPConfig).
    # Must match the topic created by Terraform (infra/modules/iceberg/main.tf).
    topic: str = "mapear-rss-raw"

    model_config = {
        "env_prefix": "MAPEAR_PUBSUB_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class AlertConfig(BaseSettings):
    """Semantic alerting config — anomaly spikes + CIB clusters.

    Drives the alert-runner Cloud Run Job (scripts/alert_runner/) that fires
    after the NLP/CIB pipeline chain (scheduled 11:00 Fortaleza).
    See docs/decisions/adr-alerting-v1.md.
    """

    enabled: bool = True
    # Spike alert: delegates threshold to mart_anomalies_daily.is_anomaly (dbt var).
    # Kept here for documentation and future override via run_alerts --min-zscore.
    spike_zscore_threshold: float = 2.0
    # CIB alert: composite_score above which a cluster is considered suspicious.
    cib_composite_score_threshold: float = 0.7
    # CIB alert: minimum days in series before alerting (avoids one-day blips).
    cib_series_age_days: int = 3
    # Slack incoming-webhook URL. Falls back to SLACK_WEBHOOK_URL if unset.
    slack_webhook_url: str = ""

    model_config = {
        "env_prefix": "MAPEAR_ALERT_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class ShadowConfig(BaseSettings):
    """Stage 1E v2 — warehouse persistence shadow config.

    When ``rule_version_yaml`` is non-empty, the RSS and social pipelines
    run a *second* PoliticalSentimentClassifier on every event using the
    candidate thresholds from the YAML, then persist the side-by-side
    result to ``silver_event_shadow``. The primary path is never altered.

    Empty YAML path = noop (CI default). Local YAMLs and ``gs://`` URIs
    are both supported by the loader; resolution lives in
    ``mapear_nlp.shadow.scorer.load_shadow_thresholds``.

    Trigger criterion (per ADR adr-shadow-scoring-stage-1e):
    >1 threshold proposal/month OR stakeholder demand for continuous
    comparison dashboard.
    """

    rule_version_yaml: str = ""
    # When False the pipeline still loads the YAML (so config errors raise
    # at startup) but skips the shadow write. Useful for dry-run validation.
    enabled: bool = True

    model_config = {
        "env_prefix": "MAPEAR_SHADOW_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class Settings(BaseSettings):
    environment: Environment = Environment.LOCAL
    data_lake_path: Path = Path("./data/lake")
    enrichment_mode: EnrichmentMode = EnrichmentMode.API
    spacy_model: str = "pt_core_news_lg"
    sentiment_model: str = "nlptown/bert-base-multilingual-uncased-sentiment"
    log_level: str = "INFO"
    log_format: str = "json"
    # Region identifier — matches a subdirectory in mapear-domain/seeds/.
    # Set MAPEAR_REGION=pe to monitor Pernambuco instead of RN.
    mapear_region: str = "rn"
    # Tenant identifier — stamped on every warehouse row written by the
    # pipelines (Stage 2B v1, data plane only). None = single-tenant /
    # "default tenant". Stamps land in raw_articles / silver_articles /
    # gold_articles / raw_social_posts / silver_social_posts /
    # raw_social_posts_dlq as a NULLABLE STRING column. Per-tenant BQ
    # dataset split / Row-Level Security / SSO / RBAC stay in the
    # control plane and are deferred to a follow-on stage (see ADR
    # docs/decisions/adr-tenant-id-stage-2b.md).
    mapear_tenant_id: str | None = None

    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    gcp: GCPConfig = Field(default_factory=GCPConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    cib: CIBConfig = Field(default_factory=CIBConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    iceberg: IcebergConfig = Field(default_factory=IcebergConfig)
    pubsub: PubSubConfig = Field(default_factory=PubSubConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)
    shadow: ShadowConfig = Field(default_factory=ShadowConfig)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _guard_enrichment_in_production(self) -> "Settings":
        if (
            self.environment == Environment.PRODUCTION
            and self.enrichment_mode == EnrichmentMode.LOCAL
        ):
            logger.warning(
                "ENRICHMENT_MODE=local in production causes OOM. "
                "Auto-correcting to ENRICHMENT_MODE=api (GCP Natural Language)."
            )
            self.enrichment_mode = EnrichmentMode.API
        return self

    @property
    def is_local(self) -> bool:
        return self.environment == Environment.LOCAL

    @property
    def lake_raw(self) -> Path:
        return self.data_lake_path / "raw"

    @property
    def lake_silver(self) -> Path:
        return self.data_lake_path / "silver"

    @property
    def lake_gold(self) -> Path:
        return self.data_lake_path / "gold"


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
