output "biglake_connection_id" {
  description = "BigLake connection ID — pass to `bq mk --external_table_definition` when creating Iceberg external tables"
  value       = google_bigquery_connection.iceberg.id
}

output "biglake_sa_email" {
  description = "Service account email that the BigLake connection uses to access GCS"
  value       = google_bigquery_connection.iceberg.cloud_resource[0].service_account_id
}

output "rss_raw_topic_id" {
  description = "Pub/Sub topic ID for streaming RSS ingestion (Eixo 1 v2)"
  value       = google_pubsub_topic.rss_raw.id
}

output "rss_raw_dlq_topic_id" {
  description = "Pub/Sub DLQ topic ID for streaming RSS ingestion"
  value       = google_pubsub_topic.rss_raw_dlq.id
}

output "iceberg_warehouse_uri" {
  description = "GCS URI that is the root of the Iceberg warehouse"
  value       = local.iceberg_warehouse_uri
}
