variable "project_id" { type = string }
variable "region" { type = string }
variable "timezone" {
  type    = string
  default = "America/Fortaleza"
}

variable "jobs" {
  description = "Map of scheduler jobs to create"
  type = map(object({
    schedule         = string
    description      = string
    cloud_run_job    = string
    cloud_run_region = string
  }))
}

resource "google_service_account" "scheduler" {
  account_id   = "mapear-scheduler"
  project      = var.project_id
  display_name = "Mapear Cloud Scheduler Service Account"
}

resource "google_project_iam_member" "scheduler_run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "etl" {
  for_each = var.jobs

  name        = each.key
  project     = var.project_id
  region      = var.region
  description = each.value.description
  schedule    = each.value.schedule
  time_zone   = var.timezone

  retry_config {
    retry_count          = 1
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${each.value.cloud_run_region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${each.value.cloud_run_job}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}

output "scheduler_service_account_email" {
  value = google_service_account.scheduler.email
}

output "job_names" {
  value = { for k, v in google_cloud_scheduler_job.etl : k => v.name }
}
