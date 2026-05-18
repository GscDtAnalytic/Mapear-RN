variable "project_id" { type = string }
variable "region" { type = string }
variable "db_password" {
  type      = string
  sensitive = true
}
variable "db_tier" { type = string }
variable "network_id" { type = string }
variable "environment" { type = string }

resource "google_sql_database_instance" "postgres" {
  name             = "mapear-rss-postgres-${var.environment}"
  project          = var.project_id
  region           = var.region
  database_version = "POSTGRES_16"

  deletion_protection = var.environment == "prod"

  settings {
    tier              = var.db_tier
    availability_type = var.environment == "prod" ? "REGIONAL" : "ZONAL"
    disk_size         = 20
    disk_type         = "PD_SSD"

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = var.environment == "prod"
      start_time                     = "03:00"
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.network_id
    }

    database_flags {
      name  = "max_connections"
      value = "100"
    }

    user_labels = {
      project     = "mapear-rss"
      environment = var.environment
    }
  }
}

resource "google_sql_database" "mapear_rn" {
  name     = "mapear_rn"
  project  = var.project_id
  instance = google_sql_database_instance.postgres.name
}


resource "google_sql_user" "mapear" {
  name     = "mapear"
  project  = var.project_id
  instance = google_sql_database_instance.postgres.name
  password = var.db_password
}

output "instance_name" {
  value = google_sql_database_instance.postgres.name
}

output "private_ip" {
  value = google_sql_database_instance.postgres.private_ip_address
}

output "connection_name" {
  value = google_sql_database_instance.postgres.connection_name
}
