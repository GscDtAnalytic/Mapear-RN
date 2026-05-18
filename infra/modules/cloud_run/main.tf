variable "project_id" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "vpc_connector_id" { type = string }

variable "jobs" {
  description = "Map of Cloud Run Jobs to create"
  type = map(object({
    image      = string
    cpu        = string
    memory     = string
    timeout    = string
    env_vars   = map(string)
    secret_env = map(string)
  }))
}

resource "google_service_account" "pipeline" {
  for_each = var.jobs

  account_id   = "mapear-${each.key}"
  project      = var.project_id
  display_name = "Mapear ${each.key} Pipeline Service Account"
}

resource "google_cloud_run_v2_job" "pipeline" {
  for_each = var.jobs

  name     = "mapear-${each.key}-pipeline"
  project  = var.project_id
  location = var.region

  template {
    template {
      containers {
        image = each.value.image

        resources {
          limits = {
            cpu    = each.value.cpu
            memory = each.value.memory
          }
        }

        dynamic "env" {
          for_each = each.value.env_vars
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = each.value.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }

      timeout = each.value.timeout
      # max_retries=0: dedup de 48h na 2ª tentativa retornaria exit(0) sem
      # trabalho real, mascarando falhas como succeededCount=1 (BL-22).
      max_retries = 0

      service_account = google_service_account.pipeline[each.key].email

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "PRIVATE_RANGES_ONLY"
      }
    }

    task_count = 1
  }

  labels = {
    project     = "mapear-rn"
    pipeline    = each.key
    environment = var.environment
  }
}

# IAM: all pipeline service accounts get GCS, BQ, and Secret Manager access
resource "google_project_iam_member" "pipeline_gcs" {
  for_each = var.jobs
  project  = var.project_id
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${google_service_account.pipeline[each.key].email}"
}

resource "google_project_iam_member" "pipeline_bq" {
  for_each = var.jobs
  project  = var.project_id
  role     = "roles/bigquery.dataEditor"
  member   = "serviceAccount:${google_service_account.pipeline[each.key].email}"
}

resource "google_project_iam_member" "pipeline_bq_job" {
  for_each = var.jobs
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.pipeline[each.key].email}"
}

resource "google_project_iam_member" "pipeline_secrets" {
  for_each = var.jobs
  project  = var.project_id
  role     = "roles/secretmanager.secretAccessor"
  member   = "serviceAccount:${google_service_account.pipeline[each.key].email}"
}

output "job_names" {
  value = { for k, v in google_cloud_run_v2_job.pipeline : k => v.name }
}

output "service_account_emails" {
  value = { for k, v in google_service_account.pipeline : k => v.email }
}
