variable "project_id" { type = string }
variable "region" { type = string }
variable "bucket_name" { type = string }
variable "environment" { type = string }

resource "google_storage_bucket" "data_lake" {
  name          = var.bucket_name
  project       = var.project_id
  location      = var.region
  force_destroy = var.environment != "prod"
  storage_class = "STANDARD"

  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  labels = {
    project     = "mapear-rss"
    environment = var.environment
  }
}

output "bucket_name" {
  value = google_storage_bucket.data_lake.name
}

output "bucket_url" {
  value = google_storage_bucket.data_lake.url
}
