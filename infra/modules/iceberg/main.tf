# Iceberg Foundation — Eixo 1 v1
#
# Provisions:
#   1. BigLake connection (CLOUD_RESOURCE) — lets BigQuery read Iceberg
#      tables on GCS via the service account that the connection creates.
#   2. IAM binding — grants the connection SA objectViewer on the GCS bucket.
#   3. BigQuery external table `silver_articles_iceberg` pointing to the
#      Iceberg metadata in GCS.  dbt can SOURCE from this table exactly like
#      an internal BQ table once the Iceberg writer is enabled.
#
# The Pub/Sub topic lives here too so the streaming consumer (Eixo 1 v2)
# can be wired without touching this module again.

locals {
  iceberg_warehouse_uri = "gs://${var.gcs_bucket_name}/${var.iceberg_gcs_prefix}"
}

# BigLake Connection (type CLOUD_RESOURCE)
# BigQuery creates a service account for the connection; we must grant it
# GCS read access so BQ can read Iceberg metadata and data files.
resource "google_bigquery_connection" "iceberg" {
  connection_id = "mapear-iceberg"
  project       = var.project_id
  location      = var.region

  cloud_resource {}
}

# Grant the BigLake connection SA objectViewer on the lake bucket.
resource "google_storage_bucket_iam_member" "biglake_reader" {
  bucket = var.gcs_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_bigquery_connection.iceberg.cloud_resource[0].service_account_id}"
}

# Grant the RSS pipeline SA connectionUser on this connection so it can
# execute CREATE OR REPLACE EXTERNAL TABLE after each Iceberg write.
# The bigquery.connections.delegate permission (in connectionUser) is only
# effective when granted at the connection resource level, not the project.
resource "google_bigquery_connection_iam_member" "rss_connection_user" {
  count         = var.rss_pipeline_sa != "" ? 1 : 0
  project       = var.project_id
  location      = var.region
  connection_id = google_bigquery_connection.iceberg.connection_id
  role          = "roles/bigquery.connectionAdmin"
  member        = "serviceAccount:${var.rss_pipeline_sa}"
}

# Grant the streaming consumer SA connectionAdmin on this connection so it can
# execute the BigLake refresh DDL after each Iceberg append.
# connectionUser only provides bigquery.connections.use; the DDL WITH CONNECTION
# clause requires bigquery.connections.delegate, which is only in connectionAdmin.
resource "google_bigquery_connection_iam_member" "stream_consumer_connection_user" {
  count         = var.stream_consumer_sa != "" ? 1 : 0
  project       = var.project_id
  location      = var.region
  connection_id = google_bigquery_connection.iceberg.connection_id
  role          = "roles/bigquery.connectionAdmin"
  member        = "serviceAccount:${var.stream_consumer_sa}"
}

# Note: The BQ external table over the Iceberg warehouse is created via
# `bq mk --table` / `bq query CREATE EXTERNAL TABLE` (one-time step in
# the ops runbook) rather than Terraform.  Reason: the `biglake_configuration`
# block in google_bigquery_table requires the google-beta provider which we
# intentionally avoid in this repo.  The BigLake connection + IAM above are
# sufficient for Terraform; the table DDL is in:
#   docs/runbooks/iceberg_biglake_setup.md

# Pub/Sub topic for streaming ingestion (Eixo 1 v2 — consumer not yet wired).
# Topic is created now so the writer-side (pipelines) can publish from day 1
# without waiting for the consumer to be implemented.
resource "google_pubsub_topic" "rss_raw" {
  name    = "mapear-rss-raw"
  project = var.project_id

  labels = {
    pipeline = "rss"
    layer    = "raw"
    eixo     = "1"
  }

  message_retention_duration = "604800s" # 7 days — allows replay on consumer failure
}

# Dead-letter topic for the streaming consumer (Eixo 1 v2).
resource "google_pubsub_topic" "rss_raw_dlq" {
  name    = "mapear-rss-raw-dlq"
  project = var.project_id

  labels = {
    pipeline = "rss"
    layer    = "raw"
    eixo     = "1"
    type     = "dlq"
  }

  message_retention_duration = "604800s"
}

# Push subscription — routes messages to the streaming consumer Cloud Run
# Service at POST /push.  Created only when stream_consumer_url is set.
# ackDeadlineSeconds=60: allows NER + sentiment (~500ms) with ample margin.
# max_delivery_attempts=5: after 5 failures the message goes to the DLQ.
resource "google_pubsub_subscription" "rss_raw_push" {
  count   = var.stream_consumer_url != "" ? 1 : 0
  name    = "mapear-rss-raw-push"
  topic   = google_pubsub_topic.rss_raw.name
  project = var.project_id

  ack_deadline_seconds = 60

  push_config {
    push_endpoint = "${var.stream_consumer_url}/push"

    oidc_token {
      service_account_email = var.stream_consumer_sa != "" ? var.stream_consumer_sa : null
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.rss_raw_dlq.id
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  labels = {
    pipeline = "rss"
    eixo     = "1"
    type     = "push"
  }
}

# Grant the streaming consumer SA subscriber rights on the DLQ topic so it
# can read and ack dead-lettered messages for investigation.
resource "google_pubsub_subscription_iam_member" "stream_consumer_dlq_subscriber" {
  count        = var.stream_consumer_sa != "" ? 1 : 0
  project      = var.project_id
  subscription = google_pubsub_subscription.rss_raw_push[0].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.stream_consumer_sa}"
}

# RSS pipeline SA needs pubsub.publisher to emit articles to the streaming topic.
resource "google_pubsub_topic_iam_member" "rss_pipeline_publisher" {
  count   = var.rss_pipeline_sa != "" ? 1 : 0
  project = var.project_id
  topic   = google_pubsub_topic.rss_raw.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${var.rss_pipeline_sa}"
}
