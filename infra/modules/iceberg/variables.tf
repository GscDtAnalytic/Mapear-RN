variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region where BigLake connection and BQ datasets live"
  type        = string
}

variable "gcs_bucket_name" {
  description = "GCS bucket that holds the Iceberg warehouse (gs://<bucket>/iceberg/)"
  type        = string
}

variable "iceberg_gcs_prefix" {
  description = "GCS path prefix under the bucket for Iceberg table data, e.g. 'iceberg/'"
  type        = string
  default     = "iceberg/"
}

variable "bq_silver_dataset" {
  description = "BigQuery dataset ID where the Iceberg external tables are registered"
  type        = string
  default     = "mapear_silver"
}

variable "rss_pipeline_sa" {
  description = "Service account email for the RSS Cloud Run Job. When set, grants roles/bigquery.connectionUser on the BigLake connection so the pipeline can execute the auto-refresh DDL."
  type        = string
  default     = ""
}

variable "stream_consumer_sa" {
  description = "Service account email for the streaming consumer Cloud Run Service. When set, grants roles/pubsub.subscriber on mapear-rss-raw."
  type        = string
  default     = ""
}

variable "stream_consumer_url" {
  description = "HTTPS URL of the streaming consumer /push endpoint. When set, creates a push subscription targeting this URL."
  type        = string
  default     = ""
}
