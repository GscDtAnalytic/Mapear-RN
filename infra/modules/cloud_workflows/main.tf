variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "location" {
  type        = string
  description = "GCP region for the workflow (must match Cloud Run Jobs region)"
}

variable "service_account_email" {
  type        = string
  description = "Service account that the workflow uses to call Cloud Run Jobs API (needs run.jobs.run on each job)"
}

variable "schedule" {
  type        = string
  description = "Cron schedule for the daily pipeline trigger (UTC)"
  default     = "0 6 * * *"
}

variable "time_zone" {
  type        = string
  description = "IANA time zone for the Cloud Scheduler trigger"
  default     = "America/Fortaleza"
}

variable "workflow_source_file" {
  type        = string
  description = "Absolute path to the Cloud Workflows YAML definition file"
}

# Enable Cloud Workflows and Cloud Scheduler APIs (idempotent)
resource "google_project_service" "workflows_api" {
  project                    = var.project_id
  service                    = "workflows.googleapis.com"
  disable_on_destroy         = false
  disable_dependent_services = false
}

resource "google_workflows_workflow" "daily_pipeline" {
  project         = var.project_id
  name            = "mapear-daily-pipeline"
  region          = var.location
  description     = "Orchestrates all Mapear-RN Cloud Run Jobs with DAG-level stage dependencies (RSS+Social → NLP+Graph → dbt → alerts+freshness)"
  service_account = var.service_account_email
  source_contents = file(var.workflow_source_file)

  depends_on = [google_project_service.workflows_api]
}

# Cloud Scheduler job that triggers the workflow once daily.
# Individual per-job schedulers are kept for manual one-off reruns.
resource "google_cloud_scheduler_job" "daily_pipeline_trigger" {
  project     = var.project_id
  region      = var.location
  name        = "mapear-daily-pipeline-trigger"
  description = "Triggers the Mapear-RN daily pipeline workflow (replaces 11 independent schedulers for full-pipeline runs)"
  schedule    = var.schedule
  time_zone   = var.time_zone

  http_target {
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.daily_pipeline.id}/executions"
    http_method = "POST"
    body        = base64encode("{}")

    oauth_token {
      service_account_email = var.service_account_email
    }
  }
}

output "workflow_id" {
  description = "Full resource ID of the Cloud Workflow"
  value       = google_workflows_workflow.daily_pipeline.id
}

output "workflow_name" {
  description = "Short name of the Cloud Workflow"
  value       = google_workflows_workflow.daily_pipeline.name
}

output "scheduler_job_name" {
  description = "Name of the Cloud Scheduler trigger job"
  value       = google_cloud_scheduler_job.daily_pipeline_trigger.name
}
