terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # Backend configurado em backend.tf (gerado pelo bootstrap_gcp.sh)
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- APIs ---
resource "google_project_service" "language" {
  project = var.project_id
  service = "language.googleapis.com"

  disable_on_destroy = false
}

# --- Rede VPC para comunicação interna ---
resource "google_compute_network" "vpc" {
  name                    = "mapear-rn-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  name          = "mapear-rn-subnet"
  ip_cidr_range = "10.0.0.0/24"
  region        = var.region
  network       = google_compute_network.vpc.id
}

# --- Firewall Rules ---
resource "google_compute_firewall" "deny_all_ingress" {
  name    = "mapear-rn-deny-all-ingress"
  network = google_compute_network.vpc.id

  priority  = 65534
  direction = "INGRESS"

  deny {
    protocol = "all"
  }

  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_firewall" "allow_internal" {
  name    = "mapear-rn-allow-internal"
  network = google_compute_network.vpc.id

  priority  = 1000
  direction = "INGRESS"

  allow {
    protocol = "tcp"
    ports    = ["5432", "6379"]
  }

  source_ranges = [google_compute_subnetwork.subnet.ip_cidr_range]
}

resource "google_compute_firewall" "allow_health_checks" {
  name    = "mapear-rn-allow-health-checks"
  network = google_compute_network.vpc.id

  priority  = 900
  direction = "INGRESS"

  allow {
    protocol = "tcp"
    ports    = ["8080", "9090"]
  }

  # GCP Health Check ranges
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
}

# --- Identity & Registry ---
module "workload_identity" {
  source      = "./modules/workload_identity"
  project_id  = var.project_id
  github_org  = var.github_org
  github_repo = var.github_repo
  environment = var.environment
}

module "artifact_registry" {
  source     = "./modules/artifact_registry"
  project_id = var.project_id
  region     = var.region
}

module "secret_manager" {
  source     = "./modules/secret_manager"
  project_id = var.project_id

  secret_ids = ["postgres-password", "redis-auth-string", "apify-token", "x-bearer-token", "mapear-llm-api-key"]

  secret_values = {
    "postgres-password"  = var.db_password
    "redis-auth-string"  = module.memorystore.auth_string
    "apify-token"        = var.apify_token
    "x-bearer-token"     = var.x_bearer_token
    "mapear-llm-api-key" = var.anthropic_api_key
  }

  accessor_keys = ["cd-sa"]
  accessor_emails = [
    module.workload_identity.cd_service_account_email,
  ]
}

# --- Networking: VPC Access Connector (Cloud Run → private resources) ---
resource "google_vpc_access_connector" "connector" {
  name          = "mapear-rn-connector"
  project       = var.project_id
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = "10.8.0.0/28"
  min_instances = 2
  max_instances = 3
  machine_type  = "e2-micro"
}

# --- Networking: Private Service Connection (Cloud SQL private IP) ---
resource "google_compute_global_address" "private_ip_range" {
  name          = "mapear-rn-private-ip"
  project       = var.project_id
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_range.name]
}

# --- Data & Compute ---
module "gcs" {
  source      = "./modules/gcs"
  project_id  = var.project_id
  region      = var.region
  bucket_name = var.lake_bucket_name
  environment = var.environment
}

module "bigquery" {
  source      = "./modules/bigquery"
  project_id  = var.project_id
  location    = var.bq_location
  environment = var.environment
}

module "cloud_sql" {
  source      = "./modules/cloud_sql"
  project_id  = var.project_id
  region      = var.region
  db_password = var.db_password
  db_tier     = var.db_tier
  network_id  = google_compute_network.vpc.id
  environment = var.environment

  depends_on = [google_service_networking_connection.private_vpc]
}

module "memorystore" {
  source     = "./modules/memorystore"
  project_id = var.project_id
  region     = var.region
  tier       = var.redis_tier
  memory_gb  = var.redis_memory_gb
  network_id = google_compute_network.vpc.id
}

# --- Cloud Run Jobs (one per ETL) ---
locals {
  shared_env_vars = {
    ENVIRONMENT           = "production"
    POSTGRES_HOST         = module.cloud_sql.private_ip
    POSTGRES_PORT         = "5432"
    POSTGRES_DB           = "mapear_rn"
    POSTGRES_USER         = "mapear"
    REDIS_HOST            = module.memorystore.host
    REDIS_PORT            = tostring(module.memorystore.port)
    REDIS_ENABLED         = "true"
    REDIS_SSL             = "true"
    GCP_GCS_BUCKET_NAME   = module.gcs.bucket_name
    GCP_PROJECT_ID        = var.project_id
    GCP_BQ_DATASET_RAW    = module.bigquery.dataset_raw_id
    GCP_BQ_DATASET_SILVER = module.bigquery.dataset_silver_id
    GCP_BQ_DATASET_GOLD   = module.bigquery.dataset_gold_id
    ENRICHMENT_MODE       = var.enrichment_mode
    # Eixo 2 v2d — narrative explainer cobre todos os sentimentos (positivo,
    # neutro, negativo), não só ALERT. Volte para "alert" para conter custo.
    MAPEAR_LLM_EXPLAINER_COVERAGE = "all"
  }

  # mapear-social não usa Postgres (sem URL Frontier) nem Redis (batch único
  # por run). Mantém GCS + BQ + projeto para o writer/loader do core.
  social_shared_env_vars = {
    ENVIRONMENT           = "production"
    GCP_GCS_BUCKET_NAME   = module.gcs.bucket_name
    GCP_PROJECT_ID        = var.project_id
    GCP_BQ_DATASET_RAW    = module.bigquery.dataset_raw_id
    GCP_BQ_DATASET_SILVER = module.bigquery.dataset_silver_id
    GCP_BQ_DATASET_GOLD   = module.bigquery.dataset_gold_id
    ENRICHMENT_MODE       = var.enrichment_mode
    # Eixo 2 v2d — ver nota em shared_env_vars.
    MAPEAR_LLM_EXPLAINER_COVERAGE = "all"
  }

  social_platforms = {
    facebook  = var.apify_actor_facebook
    instagram = var.apify_actor_instagram
    x         = var.apify_actor_x
    tiktok    = var.apify_actor_tiktok
  }

  # Diário, espaçado 1h entre plataformas para suavizar quota Apify.
  social_schedules = {
    facebook  = "0 5 * * *"
    instagram = "0 6 * * *"
    x         = "0 7 * * *"
    tiktok    = "0 8 * * *"
  }
}

module "cloud_run" {
  source           = "./modules/cloud_run"
  project_id       = var.project_id
  region           = var.region
  environment      = var.environment
  vpc_connector_id = google_vpc_access_connector.connector.id

  jobs = {
    rss = {
      image   = var.rss_pipeline_image
      cpu     = "2"
      memory  = "4Gi"
      timeout = "1800s"
      env_vars = merge(local.shared_env_vars, {
        SCRAPER_PLAYWRIGHT_ENABLED = "true"
        SCRAPER_CAMOUFOX_ENABLED   = "true"
        # FORCE_SCRAPE: when set to "true", bypasses per-domain cooldown so
        # blocked domains are retried unconditionally. Only set for manual
        # backfill runs — leave unset (or "false") for scheduled executions.
        FORCE_SCRAPE = var.rss_force_scrape
        # Eixo 1 v1 — Iceberg lakehouse. Defaults false until iceberg_enabled=true.
        MAPEAR_ICEBERG_ENABLED            = var.iceberg_enabled ? "true" : "false"
        MAPEAR_ICEBERG_WAREHOUSE          = "gs://${module.gcs.bucket_name}/${var.iceberg_gcs_prefix}"
        MAPEAR_ICEBERG_BIGLAKE_CONNECTION = var.iceberg_enabled ? "mapear-iceberg" : ""
        # Eixo 1 v2 — Pub/Sub streaming publisher. Enabled only when the
        # consumer is deployed so the topic has a subscriber.
        MAPEAR_PUBSUB_ENABLED             = var.stream_consumer_enabled ? "true" : "false"
        MAPEAR_PUBSUB_TOPIC               = "mapear-rss-raw"
      })
      secret_env = merge(
        {
          POSTGRES_PASSWORD  = "postgres-password"
          REDIS_PASSWORD     = "redis-auth-string"
          MAPEAR_LLM_API_KEY = "mapear-llm-api-key"
        },
        var.iceberg_enabled ? {
          MAPEAR_ICEBERG_CATALOG_URI = "mapear-iceberg-catalog-uri"
        } : {}
      )
    }

    freshness-emitter = {
      image   = var.freshness_emitter_image
      cpu     = "1"
      memory  = "512Mi"
      timeout = "300s"
      env_vars = {
        GCP_PROJECT_ID = var.project_id
      }
      secret_env = {}
    }

    social-facebook = {
      image   = var.social_pipeline_image
      cpu     = "1"
      memory  = "2Gi"
      timeout = "1500s"
      env_vars = merge(local.social_shared_env_vars, {
        SOCIAL_PLATFORM = "facebook"
        APIFY_ACTOR_ID  = local.social_platforms["facebook"]
      })
      secret_env = {
        APIFY_TOKEN        = "apify-token"
        MAPEAR_LLM_API_KEY = "mapear-llm-api-key"
      }
    }

    social-instagram = {
      image   = var.social_pipeline_image
      cpu     = "1"
      memory  = "2Gi"
      timeout = "1500s"
      env_vars = merge(local.social_shared_env_vars, {
        SOCIAL_PLATFORM = "instagram"
        APIFY_ACTOR_ID  = local.social_platforms["instagram"]
      })
      secret_env = {
        APIFY_TOKEN        = "apify-token"
        MAPEAR_LLM_API_KEY = "mapear-llm-api-key"
      }
    }

    social-x = {
      image   = var.social_pipeline_image
      cpu     = "1"
      memory  = "2Gi"
      timeout = "1500s"
      env_vars = merge(local.social_shared_env_vars, {
        SOCIAL_PLATFORM = "x"
      })
      secret_env = {
        X_BEARER_TOKEN     = "x-bearer-token"
        MAPEAR_LLM_API_KEY = "mapear-llm-api-key"
      }
    }

    social-tiktok = {
      image   = var.social_pipeline_image
      cpu     = "1"
      memory  = "2Gi"
      timeout = "1500s"
      env_vars = merge(local.social_shared_env_vars, {
        SOCIAL_PLATFORM = "tiktok"
        APIFY_ACTOR_ID  = local.social_platforms["tiktok"]
      })
      secret_env = {
        APIFY_TOKEN        = "apify-token"
        MAPEAR_LLM_API_KEY = "mapear-llm-api-key"
      }
    }

    # dbt build — materializes Silver → Gold (fct_content, fct_content_gold,
    # mapear_events, dim_*) in BigQuery. Runs after the last social pipeline
    # so every Gold refresh sees the day's social batches. Without this job
    # mapear_gold.* stays empty (confirmed by the 2026-04-24 BQ dump audit).
    dbt = {
      image   = var.dbt_pipeline_image
      cpu     = "2"
      memory  = "4Gi"
      timeout = "1800s"
      env_vars = {
        ENVIRONMENT    = "production"
        GCP_PROJECT_ID = var.project_id
        DBT_TARGET     = "prod"
      }
      secret_env = {}
    }

    # Eixo 3 v2b — cross-platform author identity resolution. Reads
    # silver_social_posts (last 30d), groups by (platform, author_handle),
    # runs the Acxiom-style resolver, appends persona-member rows to
    # silver_author_personas. Out-of-band (daily) on purpose: persona
    # stability is a daily property, not a per-batch one.
    graph-resolve-personas = {
      image   = var.graph_pipeline_image
      cpu     = "1"
      memory  = "2Gi"
      timeout = "1200s"
      env_vars = {
        GRAPH_JOB                  = "resolve-personas"
        GCP_PROJECT_ID             = var.project_id
        GCP_BQ_DATASET_SILVER      = module.bigquery.dataset_silver_id
        MAPEAR_REGION              = var.mapear_region
        MAPEAR_GRAPH_LOOKBACK_DAYS = "30"
        MAPEAR_CIB_ER_AUDIT_ENABLED = "true"
      }
      secret_env = {}
    }

    # Eixo 3 v2a — community detection over the author co-activation
    # graph (Louvain). Reads silver_author_activations (last 2d to cover
    # the 24h synchrony window with boundary slack); when
    # MAPEAR_CIB_USE_PERSONAS=true the latest silver_author_personas
    # snapshot is also pulled and passed via --personas to collapse
    # cross-platform duplicates into one graph node. Appends
    # silver_author_communities.
    # Eixo 2 v2a — narrative embedding + clustering. Reads gold_articles
    # where narrative_summary IS NOT NULL, embeds with sentence-transformers
    # (GCS content-addressed cache), clusters with HDBSCAN, appends to
    # silver_narrative_embeddings and silver_narrative_clusters.
    # Runs after dbt (09:00) so the day's gold_articles are fresh.
    nlp-cluster = {
      image   = var.nlp_pipeline_image
      cpu     = "2"
      memory  = "4Gi"
      timeout = "1800s"
      env_vars = {
        NLP_JOB                                = "cluster-narratives"
        GCP_PROJECT_ID                         = var.project_id
        GCP_BQ_DATASET_GOLD                    = module.bigquery.dataset_gold_id
        GCP_BQ_DATASET_SILVER                  = module.bigquery.dataset_silver_id
        GCP_GCS_BUCKET_NAME                    = module.gcs.bucket_name
        MAPEAR_REGION                          = var.mapear_region
        MAPEAR_EMBEDDINGS_CLUSTER_ALGORITHM    = "hdbscan"
        MAPEAR_EMBEDDINGS_CACHE_GCS_PREFIX     = "narrative_embeddings/"
      }
      secret_env = {}
    }

    # Eixo 2 v2b — few-shot LLM stance classification. Reads the same
    # gold_articles extract (narrative_summary IS NOT NULL), classifies
    # each narrative as favor/contra/neutro toward its target official,
    # appends to silver_article_stances. Runs after nlp-cluster (10:00)
    # so both jobs share the same gold snapshot without racing.
    nlp-stance = {
      image   = var.nlp_pipeline_image
      cpu     = "1"
      memory  = "2Gi"
      timeout = "1800s"
      env_vars = {
        NLP_JOB               = "classify-stances"
        GCP_PROJECT_ID        = var.project_id
        GCP_BQ_DATASET_GOLD   = module.bigquery.dataset_gold_id
        GCP_BQ_DATASET_SILVER = module.bigquery.dataset_silver_id
        GCP_GCS_BUCKET_NAME   = module.gcs.bucket_name
        MAPEAR_REGION         = var.mapear_region
        MAPEAR_LLM_STANCE_ENABLED          = "true"
        MAPEAR_LLM_STANCE_CACHE_GCS_PREFIX = "narrative_stance/"
        MAPEAR_LLM_PII_LEVEL               = "masked"
      }
      secret_env = {
        MAPEAR_LLM_API_KEY = "mapear-llm-api-key"
      }
    }

    # Eixo 2 v2d — LLM mayor endorsement investigation. Builds per-mayor
    # evidence bundles from mapear_events (90d co-mention window), asks
    # Sonnet to judge political alignment toward a gubernatorial candidate,
    # appends to silver_mayor_endorsements. Low-volume (~23 mayors), runs
    # after nlp-stance (11:00) so it shares the day's fresh gold snapshot.
    nlp-endorsement = {
      image   = var.nlp_pipeline_image
      cpu     = "1"
      memory  = "2Gi"
      timeout = "1800s"
      env_vars = {
        NLP_JOB               = "investigate-endorsements"
        GCP_PROJECT_ID        = var.project_id
        GCP_BQ_DATASET_GOLD   = module.bigquery.dataset_gold_id
        GCP_BQ_DATASET_SILVER = module.bigquery.dataset_silver_id
        GCP_GCS_BUCKET_NAME   = module.gcs.bucket_name
        MAPEAR_REGION         = var.mapear_region
        MAPEAR_LLM_ENDORSEMENT_ENABLED          = "true"
        MAPEAR_LLM_ENDORSEMENT_MODEL            = "claude-sonnet-4-6"
        MAPEAR_LLM_ENDORSEMENT_CACHE_GCS_PREFIX = "mayor_endorsement/"
        MAPEAR_LLM_PII_LEVEL                    = "masked"
      }
      secret_env = {
        MAPEAR_LLM_API_KEY = "mapear-llm-api-key"
      }
    }

    # Key shortened from "graph-detect-communities" so the derived
    # service-account id "mapear-${each.key}" fits GCP's 30-char limit
    # for SA account_ids. The orchestrator switch on GRAPH_JOB is what
    # actually drives behaviour — the job name is cosmetic.
    graph-communities = {
      image   = var.graph_pipeline_image
      cpu     = "2"
      memory  = "4Gi"
      timeout = "1200s"
      env_vars = {
        GRAPH_JOB                       = "detect-communities"
        GCP_PROJECT_ID                  = var.project_id
        GCP_BQ_DATASET_SILVER           = module.bigquery.dataset_silver_id
        MAPEAR_REGION                   = var.mapear_region
        MAPEAR_GRAPH_LOOKBACK_DAYS      = "2"
        MAPEAR_CIB_USE_PERSONAS         = "true"
        MAPEAR_CIB_COMMUNITY_ALGORITHM  = "louvain"
        MAPEAR_CIB_COMMUNITY_MIN_SIZE   = "3"
        MAPEAR_CIB_V3_SCORES_ENABLED      = "true"
        MAPEAR_CIB_V3_EMBEDDINGS_ENABLED  = "true"
      }
      secret_env = {}
    }

    # Eixo 2 v2a social — social post embeddings for CIB content-similarity.
    # Reads silver_social_posts (last 2 days), embeds SilverSocialPost.text
    # with paraphrase-multilingual-mpnet-base-v2 (GCS content-addressed cache),
    # appends to silver_social_post_embeddings. Out-of-band (daily) so the
    # 500 MB model load does not slow the social pipelines.
    # Runs at 09:15 — after all social pipelines complete (last: TikTok 08:00)
    # and before graph-communities consumes the table (10:00).
    # graph-communities uses this table only when MAPEAR_CIB_V3_EMBEDDINGS_ENABLED=true.
    embed-social = {
      image   = var.embed_social_runner_image
      cpu     = "2"
      memory  = "4Gi"
      timeout = "1800s"
      env_vars = {
        GCP_PROJECT_ID                                 = var.project_id
        GCP_BQ_DATASET_SILVER                          = module.bigquery.dataset_silver_id
        GCP_GCS_BUCKET_NAME                            = module.gcs.bucket_name
        MAPEAR_REGION                                  = var.mapear_region
        MAPEAR_EMBED_SOCIAL_LOOKBACK_DAYS              = "2"
        MAPEAR_EMBEDDINGS_SOCIAL_POST_CACHE_GCS_PREFIX = "social_post_embeddings/"
      }
      secret_env = {}
    }

    # Semantic alerting — fires 30 min after the last NLP job (stance, 10:30)
    # so all scores and stances are committed before the gold marts are read.
    # Queries mart_anomalies_daily (spikes) and fct_community_score_daily +
    # fct_cluster_series (CIB). Sends Slack notifications when thresholds are met.
    alert-runner = {
      image   = var.alert_runner_image
      cpu     = "1"
      memory  = "512Mi"
      timeout = "300s"
      env_vars = {
        GCP_PROJECT_ID        = var.project_id
        GCP_BQ_DATASET_GOLD   = module.bigquery.dataset_gold_id
        MAPEAR_REGION         = var.mapear_region
        MAPEAR_ALERT_ENABLED  = "true"
      }
      secret_env = {
        MAPEAR_ALERT_SLACK_WEBHOOK_URL = "slack-webhook-url"
      }
    }
  }
}

# The emitter writes custom.googleapis.com/mapear/freshness_minutes; the
# base cloud_run module grants only BQ/GCS/Secret Manager, so add the
# metric-writer role explicitly (BL-M02 housing).
resource "google_project_iam_member" "freshness_emitter_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${module.cloud_run.service_account_emails["freshness-emitter"]}"
}

# --- Cloud Scheduler (replaces Composer) ---
module "cloud_scheduler" {
  source     = "./modules/cloud_scheduler"
  project_id = var.project_id
  region     = var.region
  timezone   = var.scheduler_timezone

  jobs = {
    mapear-rss-trigger = {
      schedule         = "0 */8 * * *"
      description      = "Trigger RSS pipeline every 8 hours"
      cloud_run_job    = "mapear-rss-pipeline"
      cloud_run_region = var.region
    }

    mapear-freshness-trigger = {
      schedule         = "*/30 * * * *"
      description      = "Emit silver/gold freshness metrics every 30 minutes (M-01/M-02 housing)"
      cloud_run_job    = "mapear-freshness-emitter-pipeline"
      cloud_run_region = var.region
    }

    mapear-social-facebook-trigger = {
      schedule         = local.social_schedules["facebook"]
      description      = "Trigger mapear-social Facebook pipeline daily at 05:00 Fortaleza"
      cloud_run_job    = "mapear-social-facebook-pipeline"
      cloud_run_region = var.region
    }

    mapear-social-instagram-trigger = {
      schedule         = local.social_schedules["instagram"]
      description      = "Trigger mapear-social Instagram pipeline daily at 06:00 Fortaleza"
      cloud_run_job    = "mapear-social-instagram-pipeline"
      cloud_run_region = var.region
    }

    mapear-social-x-trigger = {
      schedule         = local.social_schedules["x"]
      description      = "Trigger mapear-social X pipeline daily at 07:00 Fortaleza"
      cloud_run_job    = "mapear-social-x-pipeline"
      cloud_run_region = var.region
    }

    mapear-social-tiktok-trigger = {
      schedule         = local.social_schedules["tiktok"]
      description      = "Trigger mapear-social TikTok pipeline daily at 08:00 Fortaleza"
      cloud_run_job    = "mapear-social-tiktok-pipeline"
      cloud_run_region = var.region
    }

    # Fires one hour after the last social job (TikTok, 08:00) so all silver
    # batches of the day land before dbt materializes Gold.
    mapear-dbt-trigger = {
      schedule         = "0 9 * * *"
      description      = "Run dbt build (seed + run + test) against BigQuery prod at 09:00 Fortaleza"
      cloud_run_job    = "mapear-dbt-pipeline"
      cloud_run_region = var.region
    }

    # Eixo 3 v2b — author resolution runs 09:30, after dbt (09:00) has
    # refreshed silver_social_posts marts. 30min slack covers the ~10min
    # dbt build with margin. v2b output feeds v2a so personas must land
    # before communities runs.
    mapear-graph-resolve-personas-trigger = {
      schedule         = "30 9 * * *"
      description      = "Eixo 3 v2b — resolve cross-platform author personas daily at 09:30 Fortaleza"
      cloud_run_job    = "mapear-graph-resolve-personas-pipeline"
      cloud_run_region = var.region
    }

    # Eixo 2 v2a — narrative clustering runs 10:00, after dbt (09:00) has
    # refreshed gold_articles. Parallel with graph-communities; both read
    # from BQ without contending on writes.
    mapear-nlp-cluster-trigger = {
      schedule         = "0 10 * * *"
      description      = "Eixo 2 v2a — embed and cluster narratives daily at 10:00 Fortaleza"
      cloud_run_job    = "mapear-nlp-cluster-pipeline"
      cloud_run_region = var.region
    }

    # Eixo 2 v2b — stance classification runs 10:30, after clustering
    # (10:00). Both jobs read from the same gold_articles snapshot;
    # 30-min offset avoids parallel heavy BQ extract queries.
    mapear-nlp-stance-trigger = {
      schedule         = "30 10 * * *"
      description      = "Eixo 2 v2b — classify narrative stance (favor/contra/neutro) daily at 10:30 Fortaleza"
      cloud_run_job    = "mapear-nlp-stance-pipeline"
      cloud_run_region = var.region
    }

    # Eixo 2 v2d — mayor endorsement investigation runs 11:00, after
    # stance (10:30). Reads the same day's mapear_events; the offset keeps
    # the heavy BQ extracts from running in parallel.
    mapear-nlp-endorsement-trigger = {
      schedule         = "0 11 * * *"
      description      = "Eixo 2 v2d — investigate mayor endorsements (LLM) daily at 11:00 Fortaleza"
      cloud_run_job    = "mapear-nlp-endorsement-pipeline"
      cloud_run_region = var.region
    }

    # Eixo 3 v2a — community detection runs 10:00, after persona
    # resolution (09:30). When MAPEAR_CIB_USE_PERSONAS is flipped on
    # the freshly-written personas table is consumed by this job.
    mapear-graph-communities-trigger = {
      schedule         = "0 10 * * *"
      description      = "Eixo 3 v2a — detect author communities (Louvain) daily at 10:00 Fortaleza"
      cloud_run_job    = "mapear-graph-communities-pipeline"
      cloud_run_region = var.region
    }

    # Eixo 2 v2a social — embed-social runs at 09:15, after all social pipelines
    # complete (TikTok starts at 08:00, typically finishes ~08:25). Runs before
    # graph-communities (10:00) so embeddings are available when the graph job
    # queries silver_social_post_embeddings (only when MAPEAR_CIB_V3_EMBEDDINGS_ENABLED=true).
    mapear-embed-social-trigger = {
      schedule         = "15 9 * * *"
      description      = "Eixo 2 v2a social — embed SilverSocialPost.text for CIB content-similarity at 09:15 Fortaleza"
      cloud_run_job    = "mapear-embed-social-pipeline"
      cloud_run_region = var.region
    }

    # Semantic alerting — fires 30 min after the last NLP/CIB jobs (stance +
    # communities, 10:30) so all fct_community_score_daily and
    # mart_anomalies_daily rows are committed before the alert queries run.
    mapear-alert-runner-trigger = {
      schedule         = "0 11 * * *"
      description      = "Semantic alerting — anomaly spikes + CIB clusters at 11:00 Fortaleza"
      cloud_run_job    = "mapear-alert-runner-pipeline"
      cloud_run_region = var.region
    }
  }
}

# --- Monitoring (Fase 4: M-01 freshness + M-11 BQ load failures + A-01) ---
module "monitoring" {
  source                              = "./modules/monitoring"
  project_id                          = var.project_id
  notification_email                  = var.alert_notification_email
  notification_email_secondary        = var.alert_notification_email_secondary
  silver_tables                       = var.silver_tables
  silver_freshness_threshold_minutes  = var.silver_freshness_threshold_minutes
  freshness_threshold_minutes_rss     = var.freshness_threshold_minutes_rss
  freshness_threshold_minutes_default = var.freshness_threshold_minutes_default
  freshness_threshold_minutes_x       = var.freshness_threshold_minutes_x
  freshness_threshold_overrides       = var.freshness_threshold_overrides
}

# --- Lakehouse Iceberg + Pub/Sub (Eixo 1 v1) ---
# Enabled when iceberg_enabled = true in prod.tfvars.
# Creates: BigLake connection, BQ external table silver_articles_iceberg,
# Pub/Sub topic mapear-rss-raw + DLQ.
module "iceberg" {
  count  = var.iceberg_enabled ? 1 : 0
  source = "./modules/iceberg"

  project_id          = var.project_id
  region              = var.region
  gcs_bucket_name     = var.lake_bucket_name
  iceberg_gcs_prefix  = var.iceberg_gcs_prefix
  bq_silver_dataset   = module.bigquery.dataset_silver_id
  rss_pipeline_sa     = module.cloud_run.service_account_emails["rss"]
  stream_consumer_sa  = var.stream_consumer_enabled ? google_service_account.stream_consumer[0].email : ""
  stream_consumer_url = var.stream_consumer_enabled ? google_cloud_run_v2_service.stream_consumer[0].uri : ""
}

# --- Streaming consumer — Cloud Run Service (Eixo 1 v2) ---
# Always-on HTTP service that receives Pub/Sub push notifications and
# processes RawArticles inline (NER + sentiment + Iceberg write).
# Enabled when stream_consumer_enabled = true in prod.tfvars.

resource "google_service_account" "stream_consumer" {
  count        = var.stream_consumer_enabled ? 1 : 0
  account_id   = "mapear-stream-consumer"
  project      = var.project_id
  display_name = "Mapear RSS Streaming Consumer Service Account"
}

resource "google_project_iam_member" "stream_consumer_bq" {
  count   = var.stream_consumer_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.stream_consumer[0].email}"
}

resource "google_project_iam_member" "stream_consumer_gcs" {
  count   = var.stream_consumer_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.stream_consumer[0].email}"
}

resource "google_project_iam_member" "stream_consumer_secret" {
  count   = var.stream_consumer_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.stream_consumer[0].email}"
}

resource "google_cloud_run_v2_service" "stream_consumer" {
  count    = var.stream_consumer_enabled ? 1 : 0
  name     = "mapear-rss-stream-consumer"
  project  = var.project_id
  location = var.region

  template {
    service_account = google_service_account.stream_consumer[0].email

    scaling {
      min_instance_count = 1  # keep NLP models warm
      max_instance_count = 3
    }

    containers {
      image = var.stream_consumer_image

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        cpu_idle          = false  # always-on for warm NLP models
        startup_cpu_boost = true
      }

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }
      env {
        name  = "MAPEAR_REGION"
        value = var.mapear_region
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_GCS_BUCKET_NAME"
        value = var.lake_bucket_name
      }
      env {
        name  = "MAPEAR_ICEBERG_ENABLED"
        value = "true"
      }
      env {
        name  = "MAPEAR_ICEBERG_WAREHOUSE"
        value = "gs://${var.lake_bucket_name}/${var.iceberg_gcs_prefix}"
      }
      env {
        name  = "MAPEAR_ICEBERG_BIGLAKE_CONNECTION"
        value = "mapear-iceberg"
      }
      env {
        name = "MAPEAR_ICEBERG_CATALOG_URI"
        value_source {
          secret_key_ref {
            secret  = "mapear-iceberg-catalog-uri"
            version = "latest"
          }
        }
      }
      env {
        name  = "POSTGRES_HOST"
        value = var.postgres_host
      }
      env {
        name = "POSTGRES_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = "postgres-password"
            version = "latest"
          }
        }
      }
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# Allow the stream-consumer SA to invoke the consumer service.
# Pub/Sub push subscription uses this SA for OIDC tokens (oidc_token.service_account_email).
resource "google_cloud_run_v2_service_iam_member" "pubsub_invoker" {
  count    = var.stream_consumer_enabled ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.stream_consumer[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.stream_consumer[0].email}"
}

# Allow the Pub/Sub system SA to impersonate the stream-consumer SA when
# generating OIDC tokens for push delivery (required by GCP push auth model).
resource "google_service_account_iam_member" "pubsub_token_creator" {
  count              = var.stream_consumer_enabled ? 1 : 0
  service_account_id = google_service_account.stream_consumer[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

data "google_project" "project" {
  project_id = var.project_id
}

# --- Looker Studio BI (Fase C / C4) ---
module "looker_bi" {
  source            = "./modules/looker_bi"
  project_id        = var.project_id
  dataset_gold_id   = module.bigquery.dataset_gold_id
  dataset_silver_id = module.bigquery.dataset_silver_id
  viewer_emails     = var.looker_viewer_emails
}

# A SA looker-bi-reader também roda o dashboard FastAPI (Cloud Run). A busca
# semântica em /narratives/search precisa da chave da Anthropic — concedemos
# acesso APENAS a este secret, mantendo a SA read-only para o resto.
resource "google_secret_manager_secret_iam_member" "dashboard_llm_key" {
  project    = var.project_id
  secret_id  = "mapear-llm-api-key"
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${module.looker_bi.service_account_email}"
  depends_on = [module.secret_manager]
}

# --- Ops KPIs (Fase 3: A-02 dup rate + A-03 volume drop) ---
module "mapear_ops" {
  source                   = "./modules/mapear_ops"
  project_id               = var.project_id
  location                 = var.bq_location
  environment              = var.environment
  dataset_gold             = module.bigquery.dataset_gold_id
  dataset_raw              = module.bigquery.dataset_raw_id
  notification_channel_id  = module.monitoring.notification_channel_id
  notification_channel_ids = module.monitoring.notification_channel_ids
  schedule_timezone        = var.scheduler_timezone
}

# --- Cloud Workflows: DAG orchestrator for the daily pipeline ---
# Declares stage dependencies (RSS+Social → NLP+Graph → dbt → alerts+freshness)
# that Cloud Scheduler's independent per-job triggers cannot express.
# Enabled via cloud_workflows_enabled in prod.tfvars.

resource "google_service_account" "pipeline_orchestrator" {
  count        = var.cloud_workflows_enabled ? 1 : 0
  account_id   = "mapear-pipeline-orchestrator"
  project      = var.project_id
  display_name = "Mapear Cloud Workflows Pipeline Orchestrator"
}

# run.jobs.run is the minimal permission needed to execute Cloud Run Jobs.
resource "google_project_iam_member" "orchestrator_run_invoker" {
  count   = var.cloud_workflows_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.pipeline_orchestrator[0].email}"
}

# Allows the orchestrator SA to poll LRO statuses via the Cloud Run API.
resource "google_project_iam_member" "orchestrator_run_viewer" {
  count   = var.cloud_workflows_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.pipeline_orchestrator[0].email}"
}

# The orchestrator SA needs to create Workflows executions (for the
# Cloud Scheduler → Workflows HTTP call to succeed).
resource "google_project_iam_member" "orchestrator_workflows_invoker" {
  count   = var.cloud_workflows_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.pipeline_orchestrator[0].email}"
}

module "cloud_workflows" {
  count  = var.cloud_workflows_enabled ? 1 : 0
  source = "./modules/cloud_workflows"

  project_id            = var.project_id
  location              = var.region
  service_account_email = google_service_account.pipeline_orchestrator[0].email
  schedule              = "0 6 * * *"
  time_zone             = var.scheduler_timezone
  workflow_source_file  = "${path.module}/workflows/mapear_daily_pipeline.yaml"

  depends_on = [
    google_project_iam_member.orchestrator_run_invoker,
    google_project_iam_member.orchestrator_run_viewer,
    google_project_iam_member.orchestrator_workflows_invoker,
  ]
}
