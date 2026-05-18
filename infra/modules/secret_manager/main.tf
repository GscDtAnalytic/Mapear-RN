# Secret Manager — Secrets for production services

variable "project_id" {
  type = string
}

variable "secret_ids" {
  description = "List of secret IDs to create"
  type        = list(string)
}

variable "secret_values" {
  description = "Map of secret_id → secret_value"
  type        = map(string)
  sensitive   = true
}

variable "accessor_emails" {
  description = "Service account emails that can read secrets"
  type        = list(string)
  default     = []
}

variable "accessor_keys" {
  description = "Static keys identifying each accessor (must match accessor_emails order)"
  type        = list(string)
  default     = []
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(var.secret_ids)
  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "versions" {
  for_each    = toset(var.secret_ids)
  secret      = google_secret_manager_secret.secrets[each.value].id
  secret_data = var.secret_values[each.value]
}

locals {
  # Build cross product using static keys so for_each is plan-time safe
  accessor_pairs = {
    for pair in setproduct(var.secret_ids, var.accessor_keys) :
    "${pair[0]}-${pair[1]}" => {
      secret_id = pair[0]
      key_index = index(var.accessor_keys, pair[1])
    }
  }
}

resource "google_secret_manager_secret_iam_member" "accessors" {
  for_each = local.accessor_pairs

  project   = var.project_id
  secret_id = google_secret_manager_secret.secrets[each.value.secret_id].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.accessor_emails[each.value.key_index]}"
}
