# Workload Identity Federation — GitHub Actions → GCP (keyless auth)
#
# Permite que GitHub Actions autentique no GCP via OIDC sem service account keys.
# Ref: https://cloud.google.com/iam/docs/workload-identity-federation

variable "project_id" {
  type = string
}

variable "github_repo" {
  description = "GitHub repository name (e.g. mapear-rss)"
  type        = string
}

variable "environment" {
  type    = string
  default = "prod"
}

# --- Workload Identity Pool ---
resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions Pool"
  description               = "Pool for GitHub Actions OIDC authentication"
}

# --- OIDC Provider (GitHub) ---
resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC Provider"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Restringir apenas ao repo autorizado
  attribute_condition = "assertion.repository == '${var.github_org}/${var.github_repo}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# --- Service Account para CI (build + test + push image) ---
resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = "github-actions-ci"
  display_name = "GitHub Actions CI"
  description  = "Used by CI pipeline to build, test, and push container images"
}

# --- Service Account para CD (deploy infra + services) ---
resource "google_service_account" "cd" {
  project      = var.project_id
  account_id   = "github-actions-cd"
  display_name = "GitHub Actions CD"
  description  = "Used by CD pipeline to deploy infrastructure and services"
}

# --- WIF binding: GitHub Actions → CI Service Account ---
resource "google_service_account_iam_member" "ci_wif" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_org}/${var.github_repo}"
}

# --- WIF binding: GitHub Actions (main branch only) → CD Service Account ---
resource "google_service_account_iam_member" "cd_wif" {
  service_account_id = google_service_account.cd.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_org}/${var.github_repo}"
}

# --- IAM Roles para CI ---
locals {
  ci_roles = [
    "roles/artifactregistry.writer", # Push Docker images
    "roles/storage.objectViewer",    # Read GCS (dbt artifacts)
  ]

  cd_roles = [
    "roles/artifactregistry.reader",      # Pull Docker images
    "roles/run.admin",                    # Deploy Cloud Run
    "roles/iam.serviceAccountUser",       # Act as service accounts
    "roles/storage.admin",                # Manage GCS (lake + dbt)
    "roles/cloudsql.admin",               # Manage Cloud SQL
    "roles/bigquery.admin",               # Manage BigQuery
    "roles/secretmanager.secretAccessor", # Read secrets
    "roles/compute.networkAdmin",         # Manage VPC/firewall
    "roles/redis.admin",                  # Manage Memorystore
  ]
}

resource "google_project_iam_member" "ci" {
  for_each = toset(local.ci_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_project_iam_member" "cd" {
  for_each = toset(local.cd_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.cd.email}"
}

# --- Outputs ---
output "workload_identity_provider" {
  description = "Full provider resource name for GitHub Actions auth"
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "ci_service_account_email" {
  description = "CI service account email"
  value       = google_service_account.ci.email
}

output "cd_service_account_email" {
  description = "CD service account email"
  value       = google_service_account.cd.email
}
