variable "project_id" { type = string }
variable "location" { type = string }
variable "environment" { type = string }

variable "dataset_gold" {
  type        = string
  description = "Gold dataset ID (source of fct_content)"
}

variable "dataset_raw" {
  type        = string
  description = "Raw dataset ID (source of raw_articles)"
}

variable "notification_channel_id" {
  type        = string
  description = "Primary notification channel for ops health alerts."
}

variable "notification_channel_ids" {
  type        = list(string)
  description = "All notification channels (primary + redundancy). Defaults to a list containing only notification_channel_id when empty (issue #40)."
  default     = []
}

locals {
  ops_notification_channels = (
    length(var.notification_channel_ids) > 0
    ? var.notification_channel_ids
    : [var.notification_channel_id]
  )
}

variable "schedule_timezone" {
  type        = string
  description = "Timezone for the scheduled query (BigQuery Data Transfer uses POSIX)"
  default     = "America/Fortaleza"
}

variable "schedule_cron" {
  type        = string
  description = "Schedule expression for the daily health query"
  default     = "every day 06:00"
}

variable "dup_rate_threshold" {
  type        = number
  description = "A-02 ASSERT trips if max(dup_rate) over the last 2d exceeds this"
  default     = 0.05
}

variable "volume_drop_threshold_ratio" {
  type        = number
  description = "A-03 ASSERT trips if raw_articles 24h / MA-7 drops below this"
  default     = 0.5
}

# --- API enablement ---
resource "google_project_service" "data_transfer" {
  project            = var.project_id
  service            = "bigquerydatatransfer.googleapis.com"
  disable_on_destroy = false
}

# --- Ops dataset ---
resource "google_bigquery_dataset" "ops" {
  dataset_id  = "mapear_ops"
  project     = var.project_id
  location    = var.location
  description = "Operational health snapshots and data-quality KPIs (A-02, A-03)"

  labels = {
    project     = "mapear-rn"
    layer       = "ops"
    environment = var.environment
  }
}

# --- Tables populated by the daily scheduled query ---
resource "google_bigquery_table" "fct_content_health" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.ops.dataset_id
  table_id            = "fct_content_health"
  deletion_protection = false

  description = "Daily snapshot of fct_content duplication rate per source_type (A-02)"

  schema = jsonencode([
    { name = "dt", type = "DATE", mode = "REQUIRED" },
    { name = "source_type", type = "STRING", mode = "REQUIRED" },
    { name = "total_rows", type = "INT64", mode = "REQUIRED" },
    { name = "distinct_ids", type = "INT64", mode = "REQUIRED" },
    { name = "dup_rate", type = "FLOAT64", mode = "REQUIRED" },
    { name = "snapshot_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])

  time_partitioning {
    type  = "DAY"
    field = "dt"
  }
}

resource "google_bigquery_table" "raw_volume_health" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.ops.dataset_id
  table_id            = "raw_volume_health"
  deletion_protection = false

  description = "Daily snapshot of raw_articles 24h volume vs 7d moving average (A-03)"

  schema = jsonencode([
    { name = "dt", type = "DATE", mode = "REQUIRED" },
    { name = "source_type", type = "STRING", mode = "REQUIRED" },
    { name = "rows_24h", type = "INT64", mode = "REQUIRED" },
    { name = "ma7_baseline", type = "FLOAT64", mode = "REQUIRED" },
    { name = "snapshot_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])

  time_partitioning {
    type  = "DAY"
    field = "dt"
  }
}

# --- Service account used by the scheduled query ---
resource "google_service_account" "ops_scheduler" {
  account_id   = "mapear-ops-scheduler"
  project      = var.project_id
  display_name = "Mapear ops scheduled-query runner"
}

resource "google_bigquery_dataset_iam_member" "ops_editor" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.ops.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ops_scheduler.email}"
}

resource "google_bigquery_dataset_iam_member" "gold_viewer" {
  project    = var.project_id
  dataset_id = var.dataset_gold
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ops_scheduler.email}"
}

resource "google_bigquery_dataset_iam_member" "raw_viewer" {
  project    = var.project_id
  dataset_id = var.dataset_raw
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ops_scheduler.email}"
}

resource "google_project_iam_member" "ops_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ops_scheduler.email}"
}

# --- Daily health scheduled query (A-02 + A-03) ---
# Single multi-statement query:
#   1. MERGE dup_rate snapshot into fct_content_health
#   2. MERGE raw volume snapshot into raw_volume_health
#   3. ASSERT dup_rate <= threshold over the last 2d (A-02)
#   4. ASSERT raw_articles 24h >= threshold_ratio * MA-7 (A-03)
# A failed ASSERT fails the transfer run → alerted via log-based metric.
locals {
  health_query = <<-SQL
    BEGIN
      MERGE `${var.project_id}.${google_bigquery_dataset.ops.dataset_id}.${google_bigquery_table.fct_content_health.table_id}` T
      USING (
        SELECT
          CURRENT_DATE('${var.schedule_timezone}') AS dt,
          source_type,
          COUNT(*) AS total_rows,
          COUNT(DISTINCT content_id) AS distinct_ids,
          SAFE_DIVIDE(COUNT(*) - COUNT(DISTINCT content_id), COUNT(*)) AS dup_rate,
          CURRENT_TIMESTAMP() AS snapshot_at
        FROM `${var.project_id}.${var.dataset_gold}.fct_content`
        GROUP BY source_type
      ) S
      ON T.dt = S.dt AND T.source_type = S.source_type
      WHEN MATCHED THEN UPDATE SET
        total_rows   = S.total_rows,
        distinct_ids = S.distinct_ids,
        dup_rate     = S.dup_rate,
        snapshot_at  = S.snapshot_at
      WHEN NOT MATCHED THEN INSERT ROW;

      MERGE `${var.project_id}.${google_bigquery_dataset.ops.dataset_id}.${google_bigquery_table.raw_volume_health.table_id}` T
      USING (
        SELECT
          CURRENT_DATE('${var.schedule_timezone}') AS dt,
          'rss' AS source_type,
          COUNTIF(extracted_at >= CURRENT_TIMESTAMP() - INTERVAL 1 DAY) AS rows_24h,
          COUNTIF(
            extracted_at >= CURRENT_TIMESTAMP() - INTERVAL 8 DAY
            AND extracted_at < CURRENT_TIMESTAMP() - INTERVAL 1 DAY
          ) / 7.0 AS ma7_baseline,
          CURRENT_TIMESTAMP() AS snapshot_at
        FROM `${var.project_id}.${var.dataset_raw}.raw_articles`
      ) S
      ON T.dt = S.dt AND T.source_type = S.source_type
      WHEN MATCHED THEN UPDATE SET
        rows_24h     = S.rows_24h,
        ma7_baseline = S.ma7_baseline,
        snapshot_at  = S.snapshot_at
      WHEN NOT MATCHED THEN INSERT ROW;

      ASSERT (
        SELECT IFNULL(MAX(dup_rate), 0)
        FROM `${var.project_id}.${google_bigquery_dataset.ops.dataset_id}.${google_bigquery_table.fct_content_health.table_id}`
        WHERE dt >= CURRENT_DATE('${var.schedule_timezone}') - INTERVAL 1 DAY
      ) <= ${var.dup_rate_threshold}
      AS 'A-02: fct_content dup_rate acima de ${var.dup_rate_threshold} nos últimos 2 dias';

      ASSERT (
        SELECT IFNULL(
          SAFE_DIVIDE(rows_24h, NULLIF(ma7_baseline, 0)),
          1.0
        )
        FROM `${var.project_id}.${google_bigquery_dataset.ops.dataset_id}.${google_bigquery_table.raw_volume_health.table_id}`
        WHERE dt = CURRENT_DATE('${var.schedule_timezone}')
          AND source_type = 'rss'
      ) >= ${var.volume_drop_threshold_ratio}
      AS 'A-03: raw_articles 24h abaixo de ${var.volume_drop_threshold_ratio} da média móvel 7d';
    END;
  SQL
}

resource "google_bigquery_data_transfer_config" "ops_daily_health" {
  project                = var.project_id
  location               = var.location
  display_name           = "mapear-ops-daily-health"
  data_source_id         = "scheduled_query"
  schedule               = var.schedule_cron
  service_account_name   = google_service_account.ops_scheduler.email
  destination_dataset_id = google_bigquery_dataset.ops.dataset_id

  params = {
    query = local.health_query
  }

  depends_on = [
    google_project_service.data_transfer,
    google_bigquery_dataset_iam_member.ops_editor,
    google_bigquery_dataset_iam_member.gold_viewer,
    google_bigquery_dataset_iam_member.raw_viewer,
    google_project_iam_member.ops_bq_job_user,
  ]
}

# --- Alert: scheduled query failure ---
# A falha do ASSERT faz o run falhar; o Data Transfer Service emite log
# severity=ERROR que é contado por esta métrica. Alertamos em 1 falha — 2 dias
# consecutivos de dup_rate > 5% já dispara naturalmente porque a ASSERT corre
# diariamente e o alert_strategy mantém o incidente aberto até resolução.
resource "google_logging_metric" "ops_health_check_failures" {
  project = var.project_id
  name    = "mapear_ops_health_check_failures"

  filter = <<-EOT
    resource.type="bigquery_dts_config"
    resource.labels.config_id="${google_bigquery_data_transfer_config.ops_daily_health.name}"
    severity=ERROR
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Mapear ops daily health-check failures"
  }
}

resource "google_monitoring_alert_policy" "ops_health_check" {
  project      = var.project_id
  display_name = "Mapear — ops daily health check failed (A-02 / A-03)"
  combiner     = "OR"
  severity     = "ERROR"

  documentation {
    content   = "Daily scheduled query `mapear-ops-daily-health` failed. The ASSERT that triggered indicates which kpi breached — see the transfer run log message for the message string. Runbooks: docs/runbooks/fct-content-dup-rate.md (A-02), docs/runbooks/raw-volume-drop.md (A-03)."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "daily health check failed"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.ops_health_check_failures.name}\" resource.type=\"bigquery_dts_config\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.ops_notification_channels

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- Pipeline freeze audit log (DoD-1) ---
# Populated via `bq query` ops step after terraform apply; never truncated.
# Each row = one freeze/unfreeze event. Dashboards filter YT data by
# freeze_date to avoid showing stale post-cutoff rows.
resource "google_bigquery_table" "freeze_log" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.ops.dataset_id
  table_id            = "freeze_log"
  deletion_protection = true

  description = "Audit log of pipeline freeze / unfreeze events (DoD-1)"

  schema = jsonencode([
    { name = "source", type = "STRING", mode = "REQUIRED",
    description = "Pipeline identifier, e.g. 'rss'" },
    { name = "frozen_at", type = "TIMESTAMP", mode = "REQUIRED",
    description = "UTC timestamp when the freeze was applied" },
    { name = "freeze_date", type = "DATE", mode = "REQUIRED",
    description = "First calendar date with no new data (use to filter dashboards)" },
    { name = "scheduler_job", type = "STRING", mode = "NULLABLE",
    description = "Cloud Scheduler job that was removed, e.g. 'mapear-rss-trigger'" },
    { name = "reason", type = "STRING", mode = "REQUIRED",
    description = "Human-readable justification for the freeze" },
    { name = "approved_by", type = "STRING", mode = "REQUIRED",
    description = "Email of approver" },
    { name = "status", type = "STRING", mode = "REQUIRED",
    description = "'FROZEN' or 'UNFROZEN'" },
  ])
}

# --- TDT-TOPIC-01: topic_id regime sentinel ---
# Monitora a proporção de topic_id = 0 em mapear_events (proxy de regime api).
# Banda esperada enquanto TDT-TOPIC-01 não é resolvido: [0.60, 0.80].
# Ver docs/tech_debt_topic_id_semantic_corruption.md para contexto completo.
variable "topic_regime_warn_low" {
  type        = number
  description = "TDT-TOPIC-01 WARN: pct_topic_zero abaixo deste valor indica mudança de regime"
  default     = 0.60
}

variable "topic_regime_warn_high" {
  type        = number
  description = "TDT-TOPIC-01 WARN: pct_topic_zero acima deste valor indica mudança de regime"
  default     = 0.80
}

resource "google_bigquery_table" "topic_id_regime_sentinel" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.ops.dataset_id
  table_id            = "topic_id_regime_sentinel"
  deletion_protection = false

  description = "Daily snapshot of topic_id=0 proportion in gold_articles (TDT-TOPIC-01 sentinel; gold_articles is RSS-only by construction — mapear_events.topic_id is hardcoded NULL on both branches)"

  schema = jsonencode([
    { name = "dt", type = "DATE", mode = "REQUIRED" },
    { name = "zero_count", type = "INT64", mode = "REQUIRED" },
    { name = "total_events", type = "INT64", mode = "REQUIRED" },
    { name = "pct_topic_zero", type = "FLOAT64", mode = "REQUIRED" },
    { name = "snapshot_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])

  time_partitioning {
    type  = "DAY"
    field = "dt"
  }
}

locals {
  topic_health_query = <<-SQL
    BEGIN
      MERGE `${var.project_id}.${google_bigquery_dataset.ops.dataset_id}.${google_bigquery_table.topic_id_regime_sentinel.table_id}` T
      USING (
        SELECT
          CURRENT_DATE('${var.schedule_timezone}') AS dt,
          COUNTIF(topic_id = 0)                    AS zero_count,
          COUNT(*)                                  AS total_events,
          SAFE_DIVIDE(COUNTIF(topic_id = 0), COUNT(*)) AS pct_topic_zero,
          CURRENT_TIMESTAMP()                       AS snapshot_at
        FROM `${var.project_id}.${var.dataset_gold}.gold_articles`
        WHERE topic_id IS NOT NULL
      ) S
      ON T.dt = S.dt
      WHEN MATCHED THEN UPDATE SET
        zero_count     = S.zero_count,
        total_events   = S.total_events,
        pct_topic_zero = S.pct_topic_zero,
        snapshot_at    = S.snapshot_at
      WHEN NOT MATCHED THEN INSERT ROW;

      ASSERT (
        SELECT IFNULL(pct_topic_zero, 0.70)
        FROM `${var.project_id}.${google_bigquery_dataset.ops.dataset_id}.${google_bigquery_table.topic_id_regime_sentinel.table_id}`
        WHERE dt = CURRENT_DATE('${var.schedule_timezone}')
      ) BETWEEN ${var.topic_regime_warn_low} AND ${var.topic_regime_warn_high}
      AS 'TDT-TOPIC-01: pct_topic_zero fora da banda [${var.topic_regime_warn_low}, ${var.topic_regime_warn_high}] — ver docs/tech_debt_topic_id_semantic_corruption.md';
    END;
  SQL
}

resource "google_bigquery_data_transfer_config" "ops_topic_health" {
  project                = var.project_id
  location               = var.location
  display_name           = "mapear-ops-topic-regime-sentinel"
  data_source_id         = "scheduled_query"
  schedule               = var.schedule_cron
  service_account_name   = google_service_account.ops_scheduler.email
  destination_dataset_id = google_bigquery_dataset.ops.dataset_id

  params = {
    query = local.topic_health_query
  }

  depends_on = [
    google_project_service.data_transfer,
    google_bigquery_dataset_iam_member.ops_editor,
    google_bigquery_dataset_iam_member.gold_viewer,
    google_project_iam_member.ops_bq_job_user,
  ]
}

resource "google_logging_metric" "topic_regime_check_failures" {
  project = var.project_id
  name    = "mapear_topic_regime_check_failures"

  filter = <<-EOT
    resource.type="bigquery_dts_config"
    resource.labels.config_id="${google_bigquery_data_transfer_config.ops_topic_health.name}"
    severity=ERROR
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Mapear topic_id regime sentinel failures"
  }
}

resource "google_monitoring_alert_policy" "topic_regime_sentinel" {
  project      = var.project_id
  display_name = "Mapear — topic_id regime fora da banda (TDT-TOPIC-01)"
  combiner     = "OR"
  severity     = "WARNING"

  documentation {
    content   = "A proporção de `topic_id = 0` em `gold_articles` saiu da banda esperada `[${var.topic_regime_warn_low}, ${var.topic_regime_warn_high}]`. Isso indica mudança de regime no pipeline de classificação de tópicos — pode ser novo volume de dados, rollout de modelo, ou regressão silenciosa. Tech debt: `docs/tech_debt_topic_id_semantic_corruption.md` (TDT-TOPIC-01). Sentinel monitora `gold_articles` (RSS-only por construção); `mapear_events.topic_id` é hardcoded NULL e não pode ser fonte deste alert. Runbook: verificar se houve deploy de nova versão do TopicClassifier ou mudança no TOPIC_ID_MAP. Não bloqueia produção, mas requer investigação antes do próximo sprint."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "topic regime sentinel failed"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.topic_regime_check_failures.name}\" resource.type=\"bigquery_dts_config\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = local.ops_notification_channels

  alert_strategy {
    auto_close = "86400s"
  }
}

output "dataset_ops_id" {
  value = google_bigquery_dataset.ops.dataset_id
}

output "scheduled_query_name" {
  value = google_bigquery_data_transfer_config.ops_daily_health.name
}

output "ops_service_account_email" {
  value = google_service_account.ops_scheduler.email
}
