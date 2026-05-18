variable "project_id" {
  description = "GCP project ID"
  type        = string
}

# --- GitHub (Workload Identity Federation) ---
variable "github_org" {
  description = "GitHub organization name"
  type        = string
  default     = "your-github-org"
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "Mapear-RN"
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "southamerica-east1"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "southamerica-east1-a"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "prod"
}

# --- Cloud SQL ---
variable "db_password" {
  description = "PostgreSQL password for Cloud SQL"
  type        = string
  sensitive   = true
}

variable "db_tier" {
  description = "Cloud SQL instance tier"
  type        = string
  default     = "db-f1-micro"
}

# --- GCS ---
variable "lake_bucket_name" {
  description = "GCS bucket name for the data lake"
  type        = string
}

# --- BigQuery ---
variable "bq_location" {
  description = "BigQuery dataset location"
  type        = string
  default     = "southamerica-east1"
}

# --- Cloud Run Jobs ---
variable "rss_pipeline_image" {
  description = "Docker image URI for the RSS pipeline (Artifact Registry)"
  type        = string
}

variable "freshness_emitter_image" {
  description = "Docker image URI for the freshness emitter job (Artifact Registry)"
  type        = string
}

variable "social_pipeline_image" {
  description = "Docker image URI for the mapear-social pipeline (Artifact Registry)"
  type        = string
}

variable "dbt_pipeline_image" {
  description = "Docker image URI for the dbt-runner Cloud Run Job (Artifact Registry). Built from scripts/dbt_runner/Dockerfile with the dbt/ project baked in."
  type        = string
}

variable "graph_pipeline_image" {
  description = "Docker image URI for the mapear-graph-runner Cloud Run Job (Artifact Registry). Built from scripts/graph_runner/Dockerfile; runs both Eixo 3 v2a (community detection) and v2b (author resolution), switched by the GRAPH_JOB env var."
  type        = string
}

variable "nlp_pipeline_image" {
  description = "Docker image URI for the mapear-nlp-runner Cloud Run Job (Artifact Registry). Built from scripts/nlp_runner/Dockerfile; runs Eixo 2 v2a (narrative clustering) and v2b (stance classification), switched by the NLP_JOB env var."
  type        = string
}

variable "alert_runner_image" {
  description = "Docker image URI for the mapear-alert-runner Cloud Run Job (Artifact Registry). Built from scripts/alert_runner/Dockerfile; queries mapear_gold marts and sends Slack notifications for anomaly spikes and sustained CIB clusters."
  type        = string
  default     = "southamerica-east1-docker.pkg.dev/your-gcp-project/mapear-rn/alert-runner:latest"
}

variable "embed_social_runner_image" {
  description = "Docker image URI for the mapear-embed-social-runner Cloud Run Job (Artifact Registry). Built from scripts/embed_social_runner/Dockerfile; embeds SilverSocialPost.text for CIB content-similarity scoring (Eixo 2 v2a social)."
  type        = string
  default     = "southamerica-east1-docker.pkg.dev/your-gcp-project/mapear-rn/embed-social-runner:latest"
}

variable "mapear_region" {
  description = "Region slug used by Mapear domain code (mapear-infra Settings.region). Defaults to 'rn'; override to expand to other states (multi-region rollout)."
  type        = string
  default     = "rn"
}

# --- Apify (mapear-social: Facebook/Instagram/TikTok scrapers) ---
variable "apify_token" {
  description = "Apify API token for mapear-social actors (BL-F2-06)"
  type        = string
  sensitive   = true
}

# --- X (Twitter) native API v2 (replaces Apify for X, 2026-04-22) ---
variable "x_bearer_token" {
  description = "X API v2 App-Only Bearer Token (X_BEARER_TOKEN) for the social-x job"
  type        = string
  sensitive   = true
}

variable "apify_actor_facebook" {
  description = "Apify actor ID for Facebook scraping"
  type        = string
  default     = "l6CUZt8H0214D3I0N"
}

variable "apify_actor_instagram" {
  description = "Apify actor ID for Instagram scraping"
  type        = string
  default     = "shu8hvrXbJbY3Eb9W"
}

variable "apify_actor_x" {
  description = "Apify actor ID for X (Twitter) scraping"
  type        = string
  default     = "ghSpYIW3L1RvT57NT"
}

variable "apify_actor_tiktok" {
  description = "Apify actor ID for TikTok scraping"
  type        = string
  default     = "5K30i8aFccKNF5ICs"
}

# --- Cloud Scheduler ---
variable "scheduler_timezone" {
  description = "Timezone for Cloud Scheduler"
  type        = string
  default     = "America/Fortaleza"
}

# --- LLM / Eixo 2 ---
variable "anthropic_api_key" {
  description = "Anthropic API key injected as mapear-llm-api-key Secret Manager secret (Eixo 2 v1 narrative explainer)"
  type        = string
  sensitive   = true
  default     = ""
}

# --- Enrichment ---
variable "enrichment_mode" {
  description = "NLP enrichment mode: skip (no NLP), api (GCP Natural Language API), local (spaCy + transformers, requires NLP image)"
  type        = string
  default     = "api"

  validation {
    condition     = contains(["skip", "api", "local"], var.enrichment_mode)
    error_message = "enrichment_mode must be one of: skip, api, local"
  }
}

# --- Memorystore ---
variable "redis_tier" {
  description = "Memorystore Redis tier"
  type        = string
  default     = "BASIC"
}

variable "redis_memory_gb" {
  description = "Redis memory size in GB"
  type        = number
  default     = 1
}

# --- Monitoring ---
variable "alert_notification_email" {
  description = "Email address that receives on-call alerts (M-01, M-11, etc.)"
  type        = string
  default     = "mapeardata@gmail.com"
}

variable "alert_notification_email_secondary" {
  description = "Optional second email channel for redundancy (issue #40). Empty disables it."
  type        = string
  default     = ""
}

variable "silver_freshness_threshold_minutes" {
  description = "Deprecated — superseded by freshness_threshold_minutes_* per-group vars."
  type        = number
  default     = 900
}

# --- Freshness monitoring — table list + per-group thresholds (A6) ---
variable "silver_tables" {
  description = "Fully qualified BQ tables (raw, silver, or gold) to monitor for freshness. Name is legacy — now covers all layers (see TD-TF-FRESH-VAR-01)."
  type        = list(string)
  default = [
    "mapear_raw.raw_articles",
    "mapear_raw.raw_social_posts_facebook",
    "mapear_raw.raw_social_posts_instagram",
    "mapear_raw.raw_social_posts_x",
    "mapear_raw.raw_social_posts_tiktok",
    "mapear_silver.silver_articles",
    "mapear_silver.silver_social_posts",
    "mapear_gold.gold_articles",
    "mapear_gold.mapear_events",
    "mapear_gold.fct_content",
    "mapear_gold.fct_content_gold",
    "mapear_gold.fct_entity_sentiment",
    "mapear_gold.fct_trends",
    "mapear_gold.dim_topics",
  ]
}

variable "freshness_threshold_minutes_rss" {
  description = "Freshness alert threshold for RSS-cadence tables (8h pipeline, 2× = 16h)"
  type        = number
  default     = 960
}

variable "freshness_threshold_minutes_default" {
  description = "Freshness alert threshold for daily-cadence tables (24h pipeline, 2× = 48h)"
  type        = number
  default     = 2880
}

variable "freshness_threshold_minutes_x" {
  description = "Freshness alert threshold for raw_social_posts_x. 72h, aligned with dbt source error_after for raw_social_posts_x; revisit when TD-X-FRESH-01 closes."
  type        = number
  default     = 4320
}

variable "freshness_threshold_overrides" {
  description = "Per-table threshold group overrides: key=table name (without dataset prefix), value=rss|default|x. Non-secret — lives here, not in prod.tfvars."
  type        = map(string)
  default = {
    "raw_articles"       = "rss"
    "silver_articles"    = "rss"
    "gold_articles"      = "rss"
    "raw_social_posts_x" = "x"
  }
}

# --- Looker Studio (Fase C / C4) ---
variable "looker_viewer_emails" {
  description = "Emails autorizados a impersonar a SA Looker BI Reader (acesso aos dashboards)."
  type        = list(string)
  default     = []
}

# --- RSS pipeline (operator overrides) ---
variable "rss_force_scrape" {
  description = "Set to 'true' to bypass per-domain cooldown in the RSS pipeline. Only for manual backfill runs — leave as 'false' for scheduled executions."
  type        = string
  default     = "false"
}

# --- Eixo 1 — Lakehouse Iceberg (desabilitado por padrão) ---
variable "iceberg_enabled" {
  description = "Enable the Iceberg module (BigLake connection + Pub/Sub topics). Set to true after the IcebergWriter is validated in prod."
  type        = bool
  default     = false
}

variable "iceberg_gcs_prefix" {
  description = "GCS path prefix for Iceberg warehouse data, e.g. 'iceberg/'. Relative to the lake bucket."
  type        = string
  default     = "iceberg/"
}

# --- Eixo 1 v2 — streaming consumer ---

variable "stream_consumer_enabled" {
  description = "Deploy the RSS streaming consumer Cloud Run Service and Pub/Sub push subscription."
  type        = bool
  default     = false
}

variable "stream_consumer_image" {
  description = "Container image URI for the streaming consumer service."
  type        = string
  default     = ""
}

variable "postgres_host" {
  description = "Cloud SQL PostgreSQL host (private IP or Cloud SQL proxy address)."
  type        = string
  default     = ""
}

# --- Cloud Workflows orchestrator ---

variable "cloud_workflows_enabled" {
  description = "Deploy the Cloud Workflows DAG orchestrator and its Cloud Scheduler trigger. Set true in prod.tfvars once the individual-scheduler approach has been validated."
  type        = bool
  default     = false
}
