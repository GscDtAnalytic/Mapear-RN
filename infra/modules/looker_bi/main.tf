# Looker Studio BI — read-only service account para conexão BQ
#
# Cria SA dedicada que o Looker Studio impersona para queries no dataset
# `mapear_gold` (marts). Sem keys expostos: o Looker Studio Pro usa
# delegated access; para Looker Studio free, gerar key manualmente via
# `gcloud iam service-accounts keys create` e fazer upload no UI.
#
# Permissões mínimas:
#   - bigquery.dataViewer no dataset gold (ler tabelas marts)
#   - bigquery.jobUser no projeto (executar queries)
#
# Ref: https://cloud.google.com/looker-studio/docs/connect-to-bigquery

variable "project_id" {
  type = string
}

variable "dataset_gold_id" {
  description = "BigQuery dataset ID que contém marts (ex: mapear_gold)"
  type        = string
}

variable "dataset_silver_id" {
  description = "BigQuery dataset ID com staging (necessário para fct_content que faz ref direto à staging)"
  type        = string
  default     = ""
}

variable "viewer_emails" {
  description = "Emails que podem impersonar a SA (acesso ao dashboard via Looker Studio)"
  type        = list(string)
  default     = []
}

# --- Service account ---
resource "google_service_account" "looker_reader" {
  project      = var.project_id
  account_id   = "looker-bi-reader"
  display_name = "Looker Studio BI Reader"
  description  = "Read-only access to BigQuery marts (mapear_gold) for Looker Studio dashboards"
}

# --- IAM: dataViewer no dataset gold ---
resource "google_bigquery_dataset_iam_member" "gold_viewer" {
  project    = var.project_id
  dataset_id = var.dataset_gold_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.looker_reader.email}"
}

# --- IAM: dataViewer no dataset silver (opcional, para fct_content sem mart agregado) ---
resource "google_bigquery_dataset_iam_member" "silver_viewer" {
  count      = var.dataset_silver_id != "" ? 1 : 0
  project    = var.project_id
  dataset_id = var.dataset_silver_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.looker_reader.email}"
}

# --- IAM: jobUser no projeto (executar queries) ---
resource "google_project_iam_member" "looker_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.looker_reader.email}"
}

# --- Acesso a impersonação para usuários autorizados ---
resource "google_service_account_iam_member" "viewers" {
  for_each = toset(var.viewer_emails)

  service_account_id = google_service_account.looker_reader.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:${each.value}"
}

# --- Outputs ---
output "service_account_email" {
  description = "Email da SA Looker (usar no UI do Looker Studio para impersonar)"
  value       = google_service_account.looker_reader.email
}
