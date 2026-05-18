variable "project_id" { type = string }
variable "location" { type = string }
variable "environment" { type = string }

variable "social_platforms" {
  type        = set(string)
  default     = ["facebook", "instagram", "x", "tiktok"]
  description = "Platforms that receive a raw_social_posts_{platform} table in mapear_raw."
}

resource "google_bigquery_dataset" "raw" {
  dataset_id = "mapear_raw"
  project    = var.project_id
  location   = var.location

  labels = {
    project     = "mapear-rss"
    layer       = "raw"
    environment = var.environment
  }
}

resource "google_bigquery_dataset" "silver" {
  dataset_id = "mapear_silver"
  project    = var.project_id
  location   = var.location

  labels = {
    project     = "mapear-rss"
    layer       = "silver"
    environment = var.environment
  }
}

resource "google_bigquery_dataset" "gold" {
  dataset_id = "mapear_gold"
  project    = var.project_id
  location   = var.location

  labels = {
    project     = "mapear-rss"
    layer       = "gold"
    environment = var.environment
  }
}

# --- Social tables (BL-F2-07) ---
#
# BL-11 lesson: tables created implicitly by BQLoader on first load drift
# silently when the pyarrow schema evolves. Social lands with tables
# pre-provisioned from a versioned JSON schema so any new field triggers
# a terraform diff (explicit migration) instead of a silent load failure.
#
# The companion drift test (mapear-core/tests/test_loaders/
# test_social_bq_schemas.py) fails CI if parquet_writer.SOCIAL_*_SCHEMA
# drifts from the JSON files.

locals {
  raw_articles_schema             = file("${path.module}/schemas/raw_articles.json")
  silver_articles_schema          = file("${path.module}/schemas/silver_articles.json")
  gold_articles_schema            = file("${path.module}/schemas/gold_articles.json")
  raw_social_schema               = file("${path.module}/schemas/raw_social_posts.json")
  silver_social_schema            = file("${path.module}/schemas/silver_social_posts.json")
  dlq_social_schema               = file("${path.module}/schemas/raw_social_posts_dlq.json")
  silver_author_activations_schema = file("${path.module}/schemas/silver_author_activations.json")
  silver_author_personas_schema    = file("${path.module}/schemas/silver_author_personas.json")
  silver_author_communities_schema = file("${path.module}/schemas/silver_author_communities.json")
  silver_narrative_embeddings_schema = file("${path.module}/schemas/silver_narrative_embeddings.json")
  silver_narrative_clusters_schema   = file("${path.module}/schemas/silver_narrative_clusters.json")
  silver_article_stances_schema      = file("${path.module}/schemas/silver_article_stances.json")
  silver_mayor_endorsements_schema   = file("${path.module}/schemas/silver_mayor_endorsements.json")
  silver_community_scores_schema          = file("${path.module}/schemas/silver_community_scores.json")
  silver_cluster_series_schema            = file("${path.module}/schemas/silver_cluster_series.json")
  silver_social_post_embeddings_schema    = file("${path.module}/schemas/silver_social_post_embeddings.json")
  silver_event_shadow_schema              = file("${path.module}/schemas/silver_event_shadow.json")
}

# --- RSS raw table (closes G-01: previously implicit-only, now Terraform-managed) ---
# The table already exists in prod (created implicitly by BQLoader on first load).
# Import before first apply:
#   terraform -chdir=infra import -var-file=prod.tfvars \
#     'module.bigquery.google_bigquery_table.raw_articles' \
#     'projects/your-gcp-project/datasets/mapear_raw/tables/raw_articles'
resource "google_bigquery_table" "raw_articles" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.raw.dataset_id
  table_id   = "raw_articles"
  schema     = local.raw_articles_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rss"
    layer       = "raw"
    environment = var.environment
  }
}

# --- RSS silver table (explicit Terraform management mirrors BL-11 lesson) ---
resource "google_bigquery_table" "silver_articles" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_articles"
  schema     = local.silver_articles_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rss"
    layer       = "silver"
    environment = var.environment
  }
}

# --- RSS gold table ---
# Same BL-11 rationale as silver_articles: previously created implicitly by
# BQLoader on first load, which let sentiment_by_entity drift to STRING while
# the pyarrow writer evolved to ARRAY<STRUCT>. Now pinned via JSON schema and
# guarded by the drift test in mapear-core/tests/test_loaders/.
resource "google_bigquery_table" "gold_articles" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.gold.dataset_id
  table_id   = "gold_articles"
  schema     = local.gold_articles_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rss"
    layer       = "gold"
    environment = "production"
  }
}

resource "google_bigquery_table" "raw_social_posts" {
  for_each = var.social_platforms

  project    = var.project_id
  dataset_id = google_bigquery_dataset.raw.dataset_id
  table_id   = "raw_social_posts_${each.value}"
  schema     = local.raw_social_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "raw"
    platform    = each.value
    environment = var.environment
  }
}

resource "google_bigquery_table" "silver_social_posts" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_social_posts"
  schema     = local.silver_social_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "silver"
    environment = var.environment
  }
}

resource "google_bigquery_table" "raw_social_posts_dlq" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.raw.dataset_id
  table_id   = "raw_social_posts_dlq"
  schema     = local.dlq_social_schema

  # DLQ is append-only audit data; deletion is expected during triage/cleanup.
  deletion_protection = false

  labels = {
    project     = "mapear-social"
    layer       = "raw"
    environment = var.environment
  }
}

# --- Eixo 3 CIB silver tables (v1 / v2a / v2b) ---
# Same BL-11 pattern: pre-provision from versioned JSON so any field
# evolution forces a terraform diff rather than silent BQLoader drift.

resource "google_bigquery_table" "silver_author_activations" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_author_activations"
  schema     = local.silver_author_activations_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "silver"
    feature     = "cib-v1"
    environment = var.environment
  }
}

resource "google_bigquery_table" "silver_author_personas" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_author_personas"
  schema     = local.silver_author_personas_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "silver"
    feature     = "cib-v2b"
    environment = var.environment
  }
}

resource "google_bigquery_table" "silver_author_communities" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_author_communities"
  schema     = local.silver_author_communities_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "silver"
    feature     = "cib-v2a"
    environment = var.environment
  }
}

# --- Eixo 2 v2a narrative clustering silver tables ---

resource "google_bigquery_table" "silver_narrative_embeddings" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_narrative_embeddings"
  schema     = local.silver_narrative_embeddings_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rss"
    layer       = "silver"
    feature     = "narrative-v2a"
    environment = var.environment
  }
}

resource "google_bigquery_table" "silver_narrative_clusters" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_narrative_clusters"
  schema     = local.silver_narrative_clusters_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rss"
    layer       = "silver"
    feature     = "narrative-v2a"
    environment = var.environment
  }
}

# --- Eixo 2 v2b stance detection silver table ---
# BL-11 pattern: pre-provision from versioned JSON so schema evolution
# forces a terraform diff rather than a silent BQLoader drift.
resource "google_bigquery_table" "silver_article_stances" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_article_stances"
  schema     = local.silver_article_stances_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rss"
    layer       = "silver"
    feature     = "eixo-2-v2b"
    environment = var.environment
  }
}

# --- Eixo 2 v2d — mayor endorsement investigation (LLM verdict) ---
# Pre-provisioned (BL-11 pattern) so schema evolution forces a terraform
# diff instead of silent drift on the next LLM-job load.
resource "google_bigquery_table" "silver_mayor_endorsements" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_mayor_endorsements"
  schema     = local.silver_mayor_endorsements_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rss"
    layer       = "silver"
    feature     = "eixo-2-v2d"
    environment = var.environment
  }
}

# --- Eixo 3 v3 — inauthenticity scoring + cluster-series persistence ---
# Same BL-11 pattern: pre-provision so schema evolution forces a terraform
# diff rather than silent drift. avg_content_similarity_score and
# jaccard_to_previous are NULLABLE by design (content-sim requires Eixo 2
# v2a embeddings; jaccard is NULL on the first day of a new series).

resource "google_bigquery_table" "silver_community_scores" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_community_scores"
  schema     = local.silver_community_scores_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "silver"
    feature     = "cib-v3"
    environment = var.environment
  }
}

resource "google_bigquery_table" "silver_cluster_series" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_cluster_series"
  schema     = local.silver_cluster_series_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "silver"
    feature     = "cib-v3"
    environment = var.environment
  }
}

# --- Eixo 2 v2a social — social post embeddings for CIB content-similarity ---
# Grain: (content_hash, embedding_model). Separate from silver_narrative_embeddings
# (which embeds LLM-generated summaries); this table embeds raw SilverSocialPost.text.
# Populated by the embed-social-runner Cloud Run Job (out-of-band, daily).
# Consumed by graph-communities when MAPEAR_CIB_V3_EMBEDDINGS_ENABLED=true.
resource "google_bigquery_table" "silver_social_post_embeddings" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_social_post_embeddings"
  schema     = local.silver_social_post_embeddings_schema

  deletion_protection = true

  labels = {
    project     = "mapear-social"
    layer       = "silver"
    feature     = "eixo-2-v2a-social"
    environment = var.environment
  }
}

# --- Stage 1E v2 — warehouse persistence shadow ---
# Grain: (content_hash, shadow_rule_version). Written inline by the RSS
# and social pipelines when MAPEAR_SHADOW_RULE_VERSION_YAML is set —
# the candidate threshold set is classified alongside the live regime
# and persisted here for continuous A/B comparison (mart_rule_version_compare).
resource "google_bigquery_table" "silver_event_shadow" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.silver.dataset_id
  table_id   = "silver_event_shadow"
  schema     = local.silver_event_shadow_schema

  deletion_protection = true

  labels = {
    project     = "mapear-rn"
    layer       = "silver"
    feature     = "stage-1e-v2"
    environment = var.environment
  }
}

output "dataset_raw_id" {
  value = google_bigquery_dataset.raw.dataset_id
}

output "dataset_silver_id" {
  value = google_bigquery_dataset.silver.dataset_id
}

output "dataset_gold_id" {
  value = google_bigquery_dataset.gold.dataset_id
}
