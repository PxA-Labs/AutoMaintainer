# The Celery worker host: one Container-Optimized OS VM without a public IP
# that runs the app image as `celery worker -B` on every queue, plus Redis
# when redis_mode is "vm". One VM, so exactly one beat scheduler runs.
#
# Why a VM and not Cloud Run: Celery workers pull from Redis and run agent
# loops for many minutes; Cloud Run services scale on and bill by HTTP
# requests and may throttle or stop an instance that has none. Cloud Run
# worker pools fit this shape better and should be re-evaluated (#527).
#
# The image is not managed here: the deploy workflow sets the metadata key
# "automaintainer-image" and the VM restarts the container (watch-image.sh).

variable "name" {
  description = "Instance name."
  type        = string
}

variable "zone" {
  type = string
}

variable "region" {
  type = string
}

variable "network" {
  description = "VPC id (firewall rules)."
  type        = string
}

variable "subnetwork" {
  description = "Subnet id."
  type        = string
}

variable "subnet_cidr" {
  description = "Range allowed to reach the self-hosted Redis."
  type        = string
}

variable "service_account_email" {
  type = string
}

variable "machine_type" {
  type = string
}

variable "disk_size_gb" {
  type = number
}

variable "registry_host" {
  description = "Artifact Registry host, e.g. us-central1-docker.pkg.dev."
  type        = string
}

variable "app_env" {
  description = "Non-secret environment of the worker container."
  type        = map(string)
}

variable "secrets" {
  description = "Environment variable name => Secret Manager secret resource name (projects/<p>/secrets/<id>)."
  type        = map(string)
}

variable "redis_url" {
  description = "Broker URL; null runs Redis on this VM and uses it."
  type        = string
  default     = null
}

variable "redis_image" {
  description = "Image of the self-hosted Redis (same as docker-compose.yml)."
  type        = string
  default     = "redis:7.4.12-alpine"
}

variable "redis_maxmemory" {
  type = string
}

variable "celery_queues" {
  type    = string
  default = "agent_runs,maintenance,github_sync,default"
}

variable "concurrency" {
  type = number
}

variable "stop_timeout_seconds" {
  type = number
}

variable "enable_iap_ssh" {
  type = bool
}

variable "deletion_protection" {
  type = bool
}

variable "labels" {
  type    = map(string)
  default = {}
}

locals {
  tag               = "${var.name}-worker"
  self_hosted_redis = var.redis_url == null

  worker_env = join("\n", [
    "REGISTRY_HOST=${var.registry_host}",
    "CELERY_QUEUES=${var.celery_queues}",
    "CELERY_CONCURRENCY=${var.concurrency}",
    "REDIS_IMAGE=${var.redis_image}",
    "REDIS_MAXMEMORY=${var.redis_maxmemory}",
    "REDIS_ALLOW_CIDR=${var.subnet_cidr}",
    "",
  ])

  container_env = merge(var.app_env, {
    REDIS_URL              = local.self_hosted_redis ? "redis://127.0.0.1:6379/0" : var.redis_url
    AUTOMAINTAINER_SECRETS = join(",", [for k, v in var.secrets : "${k}=${v}"])
  })

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    self_hosted_redis = local.self_hosted_redis
    stop_timeout      = var.stop_timeout_seconds
    worker_env        = base64encode(local.worker_env)
    app_env           = base64encode(join("", [for k, v in local.container_env : "${k}=${v}\n"]))
    secrets_exec      = filebase64("${path.module}/secrets_exec.py")
    run_worker        = filebase64("${path.module}/run-worker.sh")
    watch_image       = filebase64("${path.module}/watch-image.sh")
    run_redis         = filebase64("${path.module}/run-redis.sh")
  })
}

# Fixed internal address: Cloud Run's REDIS_URL points here in "vm" mode.
resource "google_compute_address" "worker" {
  name         = "${var.name}-worker-internal"
  region       = var.region
  subnetwork   = var.subnetwork
  address_type = "INTERNAL"
}

resource "google_compute_instance" "worker" {
  name                      = "${var.name}-worker"
  zone                      = var.zone
  machine_type              = var.machine_type
  tags                      = [local.tag]
  labels                    = var.labels
  allow_stopping_for_update = true
  deletion_protection       = var.deletion_protection

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
      size  = var.disk_size_gb
      type  = "pd-balanced"
    }
  }

  # No access_config block: no public IP. Egress goes through Cloud NAT.
  network_interface {
    subnetwork = var.subnetwork
    network_ip = google_compute_address.worker.address
  }

  service_account {
    email  = var.service_account_email
    scopes = ["cloud-platform"] # IAM roles on the service account decide.
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    user-data                 = local.user_data
    google-logging-enabled    = "true"
    google-monitoring-enabled = "true"
    enable-oslogin            = "TRUE"
    block-project-ssh-keys    = "TRUE"
    # Set by the deploy workflow; empty until the first deploy.
    automaintainer-image = ""
  }

  lifecycle {
    ignore_changes = [
      metadata["automaintainer-image"],
      # cos-stable moves on; a new image must not recreate the VM.
      boot_disk[0].initialize_params[0].image,
    ]
  }
}

resource "google_compute_firewall" "redis" {
  count         = local.self_hosted_redis ? 1 : 0
  name          = "${var.name}-allow-redis"
  network       = var.network
  direction     = "INGRESS"
  source_ranges = [var.subnet_cidr]
  target_tags   = [local.tag]

  allow {
    protocol = "tcp"
    ports    = ["6379"]
  }
}

resource "google_compute_firewall" "iap_ssh" {
  count         = var.enable_iap_ssh ? 1 : 0
  name          = "${var.name}-allow-iap-ssh"
  network       = var.network
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"] # Identity-Aware Proxy TCP forwarding
  target_tags   = [local.tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

output "instance_name" {
  value = google_compute_instance.worker.name
}

output "instance_id" {
  value = google_compute_instance.worker.instance_id
}

output "internal_ip" {
  value = google_compute_address.worker.address
}
