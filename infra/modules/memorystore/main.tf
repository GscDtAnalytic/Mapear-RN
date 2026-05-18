variable "project_id" { type = string }
variable "region" { type = string }
variable "tier" { type = string }
variable "memory_gb" { type = number }
variable "network_id" { type = string }
variable "redis_auth_enabled" {
  type    = bool
  default = true
}

resource "random_password" "redis_auth" {
  length  = 32
  special = false
}

resource "google_redis_instance" "cache" {
  name           = "mapear-rss-redis"
  project        = var.project_id
  region         = var.region
  tier           = var.tier
  memory_size_gb = var.memory_gb
  redis_version  = "REDIS_7_0"

  authorized_network = var.network_id
  auth_enabled       = var.redis_auth_enabled

  redis_configs = {
    maxmemory-policy = "allkeys-lru"
  }

  transit_encryption_mode = "SERVER_AUTHENTICATION"

  labels = {
    project = "mapear-rss"
  }
}

output "host" {
  value = google_redis_instance.cache.host
}

output "port" {
  value = google_redis_instance.cache.port
}

output "auth_string" {
  value     = google_redis_instance.cache.auth_string
  sensitive = true
}
