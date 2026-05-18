output "gcs_bucket_name" {
  description = "Data lake GCS bucket name"
  value       = module.gcs.bucket_name
}

output "gcs_bucket_url" {
  description = "Data lake GCS bucket URL"
  value       = module.gcs.bucket_url
}

output "bigquery_dataset_raw" {
  description = "BigQuery raw dataset ID"
  value       = module.bigquery.dataset_raw_id
}

output "bigquery_dataset_silver" {
  description = "BigQuery silver dataset ID"
  value       = module.bigquery.dataset_silver_id
}

output "bigquery_dataset_gold" {
  description = "BigQuery gold dataset ID"
  value       = module.bigquery.dataset_gold_id
}

output "cloud_sql_instance" {
  description = "Cloud SQL instance name"
  value       = module.cloud_sql.instance_name
}

output "cloud_sql_private_ip" {
  description = "Cloud SQL private IP"
  value       = module.cloud_sql.private_ip
}

output "redis_host" {
  description = "Memorystore Redis host"
  value       = module.memorystore.host
}

output "redis_port" {
  description = "Memorystore Redis port"
  value       = module.memorystore.port
}

# --- Cloud Run Jobs ---
output "cloud_run_job_names" {
  description = "Cloud Run Job names by ETL"
  value       = module.cloud_run.job_names
}

output "cloud_run_service_accounts" {
  description = "Cloud Run service account emails by ETL"
  value       = module.cloud_run.service_account_emails
}

# --- Cloud Scheduler ---
output "scheduler_job_names" {
  description = "Cloud Scheduler job names"
  value       = module.cloud_scheduler.job_names
}

# --- Workload Identity Federation ---
output "workload_identity_provider" {
  description = "WIF provider for GitHub Actions (use in workflow)"
  value       = module.workload_identity.workload_identity_provider
}

output "ci_service_account" {
  description = "CI service account email"
  value       = module.workload_identity.ci_service_account_email
}

output "cd_service_account" {
  description = "CD service account email"
  value       = module.workload_identity.cd_service_account_email
}

output "artifact_registry_url" {
  description = "Docker repository URL"
  value       = module.artifact_registry.repository_url
}

# --- Ops (Fase 3) ---
output "ops_dataset_id" {
  description = "Operational KPI dataset (fct_content_health, raw_volume_health)"
  value       = module.mapear_ops.dataset_ops_id
}

output "ops_scheduled_query_name" {
  description = "Daily ops health scheduled query (A-02 + A-03)"
  value       = module.mapear_ops.scheduled_query_name
}
