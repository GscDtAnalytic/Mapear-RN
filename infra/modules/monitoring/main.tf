variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "notification_email" {
  type        = string
  description = "Email address that receives alerting notifications"
}

variable "notification_email_secondary" {
  type        = string
  description = "Optional second email channel for redundancy (issue #40). Empty string disables it."
  default     = ""
}

variable "silver_tables" {
  type = list(string)
  # Covers silver and gold marts — name kept for backwards compat with alert policy resource IDs.
  description = "Fully qualified tables (silver or gold) whose freshness is alerted on"
  default = [
    "mapear_silver.silver_articles",
    "mapear_silver.silver_social_posts",
    "mapear_gold.mapear_events",
  ]
}

variable "silver_freshness_threshold_minutes" {
  type        = number
  description = "Deprecated — superseded by freshness_threshold_minutes_* per-group vars. Kept for backwards compat."
  default     = 900
}

variable "freshness_threshold_minutes_rss" {
  type        = number
  description = "Freshness alert threshold for RSS-cadence tables (8h pipeline, 2× = 16h)"
  default     = 960
}

variable "freshness_threshold_minutes_default" {
  type        = number
  description = "Freshness alert threshold for daily-cadence tables (24h pipeline, 2× = 48h)"
  default     = 2880
}

variable "freshness_threshold_minutes_x" {
  type        = number
  description = "Freshness alert threshold for raw_social_posts_x. 72h until TD-X-FRESH-01 heartbeat closes."
  default     = 4320
}

variable "freshness_threshold_overrides" {
  type        = map(string)
  description = "Per-table threshold group: key=table name (without dataset prefix), value=rss|default|x. Tables absent from map use 'default'."
  default     = {}
}

# --- Notification channel: email to on-call ---
resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "Mapear on-call email"
  type         = "email"

  labels = {
    email_address = var.notification_email
  }
}

# --- Notification channel: secondary email (redundancy, issue #40) ---
# Created only when var.notification_email_secondary is non-empty so the
# default deployment keeps a single channel exactly as before.
resource "google_monitoring_notification_channel" "email_secondary" {
  count        = var.notification_email_secondary != "" ? 1 : 0
  project      = var.project_id
  display_name = "Mapear on-call email (secondary)"
  type         = "email"

  labels = {
    email_address = var.notification_email_secondary
  }
}

locals {
  notification_channel_ids = compact([
    google_monitoring_notification_channel.email.id,
    try(google_monitoring_notification_channel.email_secondary[0].id, ""),
  ])
}

# --- M-11: BQ load failures via log-based counter ---
# Pipeline logs "BQ load failed for <table>: <err>" whenever the warehouse
# loader raises. Aggregating those log entries into a counter lets us alert
# without adding Cloud Monitoring SDK calls to the pipeline itself.
resource "google_logging_metric" "bq_load_failures" {
  project = var.project_id
  name    = "mapear_bq_load_failures"

  filter = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="mapear-rss-pipeline"
    jsonPayload.record.message:"BQ load failed"
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Mapear BQ load failures"
  }
}

resource "google_monitoring_alert_policy" "bq_load_failures" {
  project      = var.project_id
  display_name = "Mapear — BQ load failures (M-11)"
  combiner     = "OR"
  severity     = "ERROR"

  documentation {
    content   = "Three or more BQ load failures in 24h. Runbook: docs/runbooks/warehouse-frozen.md. Root cause of incident 2026-04-18 (warehouse frozen 17h)."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "bq_load_failures ≥ 3 in 24h"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.bq_load_failures.name}\" resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 2
      duration        = "0s"

      aggregations {
        alignment_period     = "86400s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "604800s" # 7d
  }
}

# --- M-01 / M-02: Silver freshness (emitted by scripts/freshness_emitter/main.py) ---
# Runs every 30 min as its own Cloud Run Job + Scheduler (see infra/main.tf);
# writes a custom gauge `custom.googleapis.com/mapear/freshness_minutes`
# labeled by table.
resource "google_monitoring_alert_policy" "silver_freshness" {
  for_each = toset(var.silver_tables)

  project      = var.project_id
  display_name = "Mapear — silver freshness stale (${each.value})"
  combiner     = "OR"
  severity     = "WARNING"

  documentation {
    content   = "Freshness for ${each.value} exceeded threshold. Runbook: docs/runbooks/silver-freshness-breach.md."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "freshness_minutes exceeded threshold for ${each.value}"

    condition_threshold {
      filter     = "metric.type=\"custom.googleapis.com/mapear/freshness_minutes\" resource.type=\"global\" metric.labels.table=\"${each.value}\""
      comparison = "COMPARISON_GT"
      threshold_value = lookup(
        {
          rss     = var.freshness_threshold_minutes_rss
          default = var.freshness_threshold_minutes_default
          x       = var.freshness_threshold_minutes_x
        },
        lookup(var.freshness_threshold_overrides, split(".", each.key)[1], "default"),
        var.freshness_threshold_minutes_default
      )
      duration = "0s"

      aggregations {
        alignment_period   = "3600s"
        per_series_aligner = "ALIGN_MEAN"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "604800s"
  }
}

# --- A-01: Schema drift detector ---
# Previne a classe de regressão do incidente 2026-04-19: BQ rejeita o Parquet
# com "Provided Schema does not match Table" quando o loader adiciona colunas
# antes do `bq update --schema` (BL-20). Alert dispara na 1ª ocorrência.
resource "google_logging_metric" "bq_schema_drift" {
  project = var.project_id
  name    = "mapear_bq_schema_drift"

  filter = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="mapear-rss-pipeline"
    jsonPayload.record.message:"Provided Schema does not match Table"
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Mapear BQ schema drift occurrences"
  }
}

resource "google_monitoring_alert_policy" "bq_schema_drift" {
  project      = var.project_id
  display_name = "Mapear — BQ schema drift detected (A-01)"
  combiner     = "OR"
  severity     = "CRITICAL"

  documentation {
    content   = "BigQuery rejected a Parquet load because its schema no longer matches the table — likely a new column in the pipeline that wasn't propagated via `bq update --schema`. Runbook: docs/runbooks/yt-schema-drift.md. Root cause of incident 2026-04-19 (warehouse frozen 30h)."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "schema_drift ≥ 1 in 5min"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.bq_schema_drift.name}\" resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- A-04: RSS pipeline execution failures ---
# Uses the built-in Cloud Run Job execution metric (no log-based metric needed).
# Fires on any execution where the container exits non-zero — covers unhandled
# exceptions (exit 1), BQ load failures (exit 2), Apify failures (exit 3),
# and config errors (exit 5). BQ-specific failures also fire M-11; both runbooks
# should be consulted in those cases.
resource "google_monitoring_alert_policy" "rss_pipeline_failed" {
  project      = var.project_id
  display_name = "Mapear — RSS pipeline execution failed (A-04)"
  combiner     = "OR"
  severity     = "ERROR"

  documentation {
    content   = "Cloud Run Job mapear-rss-pipeline completed with result=failed (container exited non-zero). Runbook: docs/runbooks/rss-pipeline-failed.md."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "rss execution result=failed"

    condition_threshold {
      filter = join(" ", [
        "metric.type=\"run.googleapis.com/job/completed_execution_count\"",
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"mapear-rss-pipeline\"",
        "metric.labels.result=\"failed\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- A-05: dbt pipeline execution failures ---
# Same metric as A-04. dbt exits non-zero on: dbt test failure (exit 2),
# dbt run failure (set -e), missing container image (ContainerMissing → exit
# non-zero from Cloud Run infrastructure, not container). Note: the job has
# been failing continuously due to missing dbt-runner image (tech_debt_dbt_
# production_never_ran); this alert will fire on the next scheduled run.
resource "google_monitoring_alert_policy" "dbt_pipeline_failed" {
  project      = var.project_id
  display_name = "Mapear — dbt pipeline execution failed (A-05)"
  combiner     = "OR"
  severity     = "ERROR"

  documentation {
    content   = "Cloud Run Job mapear-dbt-pipeline completed with result=failed. Distinguish dbt test failure (exit 2) from dbt run/seed failure (exit 1) via job logs. Runbook: docs/runbooks/dbt-pipeline-failed.md."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "dbt execution result=failed"

    condition_threshold {
      filter = join(" ", [
        "metric.type=\"run.googleapis.com/job/completed_execution_count\"",
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"mapear-dbt-pipeline\"",
        "metric.labels.result=\"failed\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- A-07 to A-17: Execution failures for all remaining Cloud Run Jobs ---
# Same metric as A-04/A-05. Previously only RSS and dbt had job-level execution
# failure alerts; the 11 remaining jobs fired M-11 (BQ log-based) or nothing.
# Root cause that motivated adding these: mapear-alert-runner-trigger pointed at
# a non-existent job name and returned HTTP 404 for every scheduled execution
# without triggering any alert (sessão 37 post-mortem).
locals {
  secondary_pipeline_alert_codes = {
    "social-facebook"   = { job = "mapear-social-facebook-pipeline",          code = "A-07" }
    "social-instagram"  = { job = "mapear-social-instagram-pipeline",         code = "A-08" }
    "social-x"          = { job = "mapear-social-x-pipeline",                 code = "A-09" }
    "social-tiktok"     = { job = "mapear-social-tiktok-pipeline",            code = "A-10" }
    "nlp-cluster"       = { job = "mapear-nlp-cluster-pipeline",              code = "A-11" }
    "nlp-stance"        = { job = "mapear-nlp-stance-pipeline",               code = "A-12" }
    "graph-personas"    = { job = "mapear-graph-resolve-personas-pipeline",   code = "A-13" }
    "graph-communities" = { job = "mapear-graph-communities-pipeline",        code = "A-14" }
    "alert-runner"      = { job = "mapear-alert-runner-pipeline",             code = "A-15" }
    "embed-social"      = { job = "mapear-embed-social-pipeline",             code = "A-16" }
    "freshness-emitter" = { job = "mapear-freshness-emitter-pipeline",        code = "A-17" }
  }
}

resource "google_monitoring_alert_policy" "pipeline_job_failed" {
  for_each = local.secondary_pipeline_alert_codes

  project      = var.project_id
  display_name = "Mapear — ${each.key} execution failed (${each.value.code})"
  combiner     = "OR"
  severity     = "ERROR"

  documentation {
    content   = "Cloud Run Job `${each.value.job}` completed with result=failed (container exited non-zero). Check job logs in Cloud Logging: `resource.type=\"cloud_run_job\" resource.labels.job_name=\"${each.value.job}\"`."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "${each.key} execution result=failed"

    condition_threshold {
      filter = join(" ", [
        "metric.type=\"run.googleapis.com/job/completed_execution_count\"",
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"${each.value.job}\"",
        "metric.labels.result=\"failed\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- A-18: Cloud Scheduler execution failures ---
# Cloud Scheduler fires HTTP against Cloud Run Jobs API. When the target job
# doesn't exist (wrong name), the API returns 404 and the scheduler logs an
# ERROR — but no execution metric fires since the job never ran. This log-based
# alert closes the blind spot exposed by the sessão 37 post-mortem where
# mapear-alert-runner-trigger returned 404 daily for ~10 sessions undetected.
resource "google_logging_metric" "scheduler_execution_failures" {
  project = var.project_id
  name    = "mapear_scheduler_execution_failures"

  filter = <<-EOT
    resource.type="cloud_scheduler_job"
    protoPayload.status.code!="0"
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Mapear Cloud Scheduler execution failures"
    labels {
      key         = "job_id"
      value_type  = "STRING"
      description = "Cloud Scheduler job ID"
    }
  }

  label_extractors = {
    "job_id" = "EXTRACT(resource.labels.job_id)"
  }
}

resource "google_monitoring_alert_policy" "scheduler_execution_failed" {
  project      = var.project_id
  display_name = "Mapear — Cloud Scheduler execution failed (A-18)"
  combiner     = "OR"
  severity     = "ERROR"

  documentation {
    content   = "A Cloud Scheduler job returned a non-zero status code. Common causes: (1) target Cloud Run Job name doesn't exist — check `infra/main.tf` scheduler blocks for typos; (2) the service account lacks `run.jobs.run` permission; (3) the job's Cloud Run region is wrong. Query: `resource.type=\"cloud_scheduler_job\" severity>=ERROR`."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "scheduler execution non-zero status"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.scheduler_execution_failures.name}\" resource.type=\"cloud_scheduler_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- A-06: Social pipeline Apify 401 auth failures ---
# Log-based: the social pipeline logs "Apify run failed: Apify 401 on <context>"
# at ERROR level (pipeline.py:742) before exiting with code 3. The execution
# metric (A-04 style) would also catch it, but this alert fires faster and
# provides platform-specific grouping for diagnosis.
# Note: X platform uses X Bearer Token (not Apify); X 401s are caught by the
# general execution metric via exit(1) from the unhandled exception path.
resource "google_logging_metric" "social_401_errors" {
  project = var.project_id
  name    = "mapear_social_401_errors"

  filter = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name:"mapear-social-"
    jsonPayload.record.message:"Apify run failed: Apify 401"
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Mapear social Apify 401 errors"
  }
}

resource "google_monitoring_alert_policy" "social_401" {
  project      = var.project_id
  display_name = "Mapear — social pipeline Apify 401 auth failures (A-06)"
  combiner     = "OR"
  severity     = "WARNING"

  documentation {
    content   = "Apify returned HTTP 401 on a social pipeline — likely expired or revoked token. Runbook: docs/runbooks/social-pipeline-401.md. Remediation via docs/runbooks/secret-rotation.md."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "social Apify 401 errors > 3 in 10min"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.social_401_errors.name}\" resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 3
      duration        = "0s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.job_name"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "10800s"
  }
}

# --- TDT-TOPIC-01: topic_id_source IS NULL sentinel (CRIT) ---
# Pipeline logs an error when it writes gold articles with topic_id_source IS NULL.
# After backfill (rollout step 4) this should never occur in new writes.
# Log message filter: "TDT-TOPIC-01 sentinel CRIT" (see Mapear-RSS/src/mapear_rss/pipeline.py).
resource "google_logging_metric" "topic_id_source_null" {
  project = var.project_id
  name    = "mapear_topic_id_source_null"

  filter = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="mapear-rss-pipeline"
    jsonPayload.record.message:"TDT-TOPIC-01 sentinel CRIT"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "batch"
      value_type  = "STRING"
      description = "Pipeline batch ID"
    }
  }

  label_extractors = {
    "batch" = "REGEXP_EXTRACT(jsonPayload.record.message, \"in batch (\\\\S+) —\")"
  }
}

resource "google_monitoring_alert_policy" "topic_id_source_null" {
  project      = var.project_id
  display_name = "TDT-TOPIC-01: topic_id_source IS NULL in gold articles (CRIT)"
  combiner     = "OR"

  conditions {
    display_name = "topic_id_source null writes"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.topic_id_source_null.name}\" resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  documentation {
    content   = "topic_id_source IS NULL found in a gold_articles write after TDT-TOPIC-01 backfill. Producer may be regressed to pre-E2 version. Check Cloud Run job logs and redeploy if necessary. See docs/decisions/adr_tdt_topic_01_remediation.md."
    mime_type = "text/markdown"
  }

  severity = "CRITICAL"

  notification_channels = local.notification_channel_ids

  alert_strategy {
    auto_close = "10800s"
  }
}

# > 10pp from rolling-14d baseline. Requires baseline values computed after 7 days
# of post-rollout production data. See docs/operational_baselines.md.

output "notification_channel_id" {
  description = "Primary email channel ID. Kept for backwards compat with mapear_ops module."
  value       = google_monitoring_notification_channel.email.id
}

output "notification_channel_ids" {
  description = "All notification channel IDs (primary + optional secondary, issue #40)."
  value       = local.notification_channel_ids
}

output "bq_load_failures_metric_name" {
  value = google_logging_metric.bq_load_failures.name
}

output "schema_drift_metric_name" {
  value = google_logging_metric.bq_schema_drift.name
}

output "social_401_metric_name" {
  value = google_logging_metric.social_401_errors.name
}

output "topic_id_source_null_metric_name" {
  value = google_logging_metric.topic_id_source_null.name
}
