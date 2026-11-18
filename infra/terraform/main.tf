# AutoMaintainer on Google Cloud (#527).
#
#   GitHub webhooks, dashboard ──HTTPS──► Cloud Run "api" (min 1 instance)
#                                            │ Direct VPC egress (private ranges)
#                                            ▼
#   worker VM (COS, no public IP) ◄──── Redis: on the VM (default) or Memorystore
#     celery worker -B, all queues           │
#     └── Cloud NAT ──► GitHub, Groq, Supabase
#
# Supabase stays the database. docs/DEPLOYMENT.md ("Google Cloud") has the
# owner's steps; README.md in this directory has the details.

locals {
  services = toset(concat(
    [
      "artifactregistry.googleapis.com",
      "billingbudgets.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "cloudtrace.googleapis.com",
      "compute.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "run.googleapis.com",
      "secretmanager.googleapis.com",
      "sts.googleapis.com",
    ],
    var.redis_mode == "memorystore" ? ["redis.googleapis.com"] : [],
  ))

  registry_host    = "${var.region}-docker.pkg.dev"
  image_repository = "${local.registry_host}/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/automaintainer"

  redis_url = (
    var.redis_mode == "memorystore"
    ? "redis://${google_redis_instance.broker[0].host}:${google_redis_instance.broker[0].port}/0"
    : "redis://${module.worker.internal_ip}:6379/0"
  )

  # Shared by the API and the worker, so MERGE_POLICY and the rest match.
  app_env = merge(var.app_env, {
    ENVIRONMENT          = "production"
    GOOGLE_CLOUD_PROJECT = var.project_id
    # Metrics come from both services: runs and LLM tokens from the worker,
    # HTTP metrics from the API (backend/observability.py).
    OTEL_CLOUD_MONITORING_ENABLED = var.cloud_monitoring_enabled ? "true" : "false"
  })
}

resource "google_project_service" "services" {
  for_each = local.services
  service  = each.value
  # Turning an API off on destroy could break things outside this config.
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  repository_id = var.name_prefix
  location      = var.region
  format        = "DOCKER"
  description   = "AutoMaintainer images, pushed by .github/workflows/deploy-gcp.yml"

  cleanup_policy_dry_run = false
  # KEEP wins over DELETE: the 20 newest images (rollback targets) stay.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }
  cleanup_policies {
    id     = "delete-older-than-30-days"
    action = "DELETE"
    condition {
      older_than = "2592000s"
    }
  }

  depends_on = [google_project_service.services]
}

module "network" {
  source      = "./modules/network"
  name        = var.name_prefix
  region      = var.region
  subnet_cidr = var.subnet_cidr

  depends_on = [google_project_service.services]
}

# Optional managed broker. Basic tier: one node, no replica, no AUTH (the
# instance is reachable only from the peered VPC). noeviction as everywhere
# else: evicting broker keys would silently drop queued tasks and locks.
resource "google_redis_instance" "broker" {
  count              = var.redis_mode == "memorystore" ? 1 : 0
  name               = "${var.name_prefix}-redis"
  tier               = "BASIC"
  memory_size_gb     = var.memorystore_size_gb
  region             = var.region
  authorized_network = module.network.network_id
  connect_mode       = "DIRECT_PEERING"
  redis_version      = "REDIS_7_2"
  redis_configs = {
    maxmemory-policy = "noeviction"
  }

  depends_on = [google_project_service.services]
}

module "worker" {
  source = "./modules/worker"

  name                  = var.name_prefix
  region                = var.region
  zone                  = var.zone
  network               = module.network.network_id
  subnetwork            = module.network.subnet_id
  subnet_cidr           = module.network.subnet_cidr
  service_account_email = google_service_account.worker.email
  machine_type          = var.worker_machine_type
  disk_size_gb          = var.worker_disk_size_gb
  concurrency           = var.worker_concurrency
  stop_timeout_seconds  = var.worker_stop_timeout_seconds
  registry_host         = local.registry_host
  enable_iap_ssh        = var.enable_iap_ssh
  deletion_protection   = var.deletion_protection
  redis_maxmemory       = var.redis_vm_maxmemory
  redis_url = (
    var.redis_mode == "memorystore"
    ? "redis://${google_redis_instance.broker[0].host}:${google_redis_instance.broker[0].port}/0"
    : null
  )
  app_env = local.app_env
  secrets = { for k, v in var.secrets : k => google_secret_manager_secret.app[k].id if v.worker }

  depends_on = [
    google_project_service.services,
    google_secret_manager_secret_iam_member.worker,
    google_artifact_registry_repository_iam_member.worker_pull,
  ]
}

module "github_wif" {
  source = "./modules/github_wif"

  pool_id                    = "github"
  github_repository          = var.github_repository
  github_repository_id       = var.github_repository_id
  github_repository_owner_id = var.github_repository_owner_id
  github_environment         = var.github_environment
  service_account_name       = google_service_account.deployer.name

  depends_on = [google_project_service.services]
}
