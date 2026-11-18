# Sandboxed verification (#158, #527): every test command of a verification
# runs as one execution of a Cloud Run job, never on the worker VM.
#
#   worker VM ── upload copy, sign 2 URLs ──► bucket  executions/<random>/
#       │      ── run job with overrides ───► Cloud Run job "<prefix>-sandbox"
#       │                                       sandbox SA: no role at all
#       │                                       egress: sandbox VPC only
#       │                                         no default route, no NAT,
#       │                                         deny-all egress firewall,
#       │                                         except 443 to
#       │                                         private.googleapis.com
#       ◄── poll, read result, delete objects ──┘
#
# backend/automaintainer/runners/cloud_run_job_sandbox.py is the worker side
# and carries the job's entrypoint in each execution's arguments.
# docs/VERIFICATION.md ("Cloud Run Jobs sandbox backend") explains the threat
# model; docs/DEPLOYMENT.md (Option D, "Sandboxed verification") has the
# owner's smoke test and the GKE Sandbox fallback.
#
# What the job's identity can and cannot do: a Cloud Run job always reaches
# its own metadata server (169.254.169.254, outside the VPC, so no firewall
# rule applies), and code in it can mint an access token for the job's
# service account. That account is therefore granted nothing: no project
# role, no bucket role, no actAs. The working copy and the result are reached
# only through V4 URLs the worker signs per execution, valid for one object
# each and for the run's duration. The metadata server is the job's own, not
# the worker VM's, so the worker's token and secrets stay out of reach.

variable "sandbox_image" {
  description = <<-EOT
    Image the verification job runs: it needs python3 (3.11.4 or later) and
    whatever the repositories' tests need (git, node, pytest...). Set it to
    the app image in Artifact Registry by digest after the first deploy,
    e.g. "<image_repository>@sha256:<digest>". The default, Google's sample
    job image, has no python3, so every verification fails closed (reports
    an error) until then.
  EOT
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/job:latest"
}

variable "sandbox_subnet_cidr" {
  description = "Range of the sandbox subnet (its own VPC). Direct VPC egress uses one IP per running task, so keep it at /26 or larger."
  type        = string
  default     = "10.20.0.0/26"
}

variable "sandbox_cpu" {
  description = "vCPUs of one verification task."
  type        = string
  default     = "1"
}

variable "sandbox_memory" {
  description = "Memory of one verification task. The working copy is unpacked into the in-memory filesystem, so it counts against this."
  type        = string
  default     = "2Gi"
}

variable "sandbox_task_timeout_seconds" {
  description = "Task timeout when an execution sets none. The worker always overrides it with the command's timeout plus a margin."
  type        = number
  default     = 1800
}

locals {
  sandbox_services = toset(["dns.googleapis.com", "storage.googleapis.com"])
}

resource "google_project_service" "sandbox" {
  for_each           = local.sandbox_services
  service            = each.value
  disable_on_destroy = false
}

# --- identity -----------------------------------------------------------------

resource "google_service_account" "sandbox" {
  account_id   = "${var.name_prefix}-sandbox"
  display_name = "AutoMaintainer verification sandbox (Cloud Run job, no roles)"
  depends_on   = [google_project_service.services]
}

# --- network: its own VPC, with no way to the internet ----------------------------

resource "google_compute_network" "sandbox" {
  name                    = "${var.name_prefix}-sandbox-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  # No 0.0.0.0/0 route: even a wrong firewall rule cannot reach the internet.
  delete_default_routes_on_create = true
  depends_on                      = [google_project_service.services]
}

resource "google_compute_subnetwork" "sandbox" {
  name                     = "${var.name_prefix}-sandbox-${var.region}"
  network                  = google_compute_network.sandbox.id
  region                   = var.region
  ip_cidr_range            = var.sandbox_subnet_cidr
  private_ip_google_access = true
}

# The only route out: Private Google Access, so the job can fetch its copy
# and upload its result from Cloud Storage.
resource "google_compute_route" "sandbox_private_google_access" {
  name             = "${var.name_prefix}-sandbox-private-google-access"
  network          = google_compute_network.sandbox.id
  dest_range       = "199.36.153.8/30"
  next_hop_gateway = "default-internet-gateway"
  priority         = 1000
}

resource "google_compute_firewall" "sandbox_allow_google_apis" {
  name               = "${var.name_prefix}-sandbox-allow-google-apis"
  network            = google_compute_network.sandbox.id
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["199.36.153.8/30"]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

# Overrides the implied allow-all egress rule (65535) for everything else:
# the worker VM, Redis, Memorystore and the internet are all unreachable
# (the worker's VPC is not peered with this one anyway).
resource "google_compute_firewall" "sandbox_deny_egress" {
  name               = "${var.name_prefix}-sandbox-deny-egress"
  network            = google_compute_network.sandbox.id
  direction          = "EGRESS"
  priority           = 65000
  destination_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }

  # Evidence for the smoke test: denied connections show up in Logging.
  log_config {
    metadata = "EXCLUDE_ALL_METADATA"
  }
}

# storage.googleapis.com resolves to private.googleapis.com inside the
# sandbox VPC. Every other Google API name resolves to public addresses,
# which have no route here.
resource "google_dns_managed_zone" "sandbox_storage" {
  name        = "${var.name_prefix}-sandbox-storage"
  dns_name    = "storage.googleapis.com."
  description = "Cloud Storage through Private Google Access for the verification sandbox."
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.sandbox.id
    }
  }

  depends_on = [google_project_service.sandbox]
}

resource "google_dns_record_set" "sandbox_storage" {
  managed_zone = google_dns_managed_zone.sandbox_storage.name
  name         = "storage.googleapis.com."
  type         = "A"
  ttl          = 300
  # private.googleapis.com (199.36.153.8/30).
  rrdatas = ["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"]
}

# --- bucket: one prefix per execution, gone within a day ------------------------------

resource "google_storage_bucket" "sandbox" {
  name                        = module.worker.sandbox_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  # Transient copies and results only; nothing to keep on destroy.
  force_destroy = true

  # The worker deletes both objects after each run; this catches what a
  # crashed worker leaves behind.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "Delete"
    }
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  depends_on = [google_project_service.sandbox]
}

# --- the job ----------------------------------------------------------------------

resource "google_cloud_run_v2_job" "sandbox" {
  name                = module.worker.sandbox_job_name
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account       = google_service_account.sandbox.email
      timeout               = "${var.sandbox_task_timeout_seconds}s"
      max_retries           = 0
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.sandbox.id
          subnetwork = google_compute_subnetwork.sandbox.id
        }
      }

      containers {
        image = var.sandbox_image
        # The worker overrides the args of each execution with
        # "-I -c <entrypoint> <test command...>".
        command = ["python3"]

        resources {
          limits = {
            cpu    = var.sandbox_cpu
            memory = var.sandbox_memory
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.services,
    google_compute_firewall.sandbox_deny_egress,
    google_compute_firewall.sandbox_allow_google_apis,
  ]
}

# --- what the worker may do ---------------------------------------------------------

# Start executions with per-execution overrides, follow and cancel them. On
# the job only; roles/run.jobsExecutorWithOverrides would also do, but this
# list says exactly what the worker uses.
resource "google_project_iam_custom_role" "sandbox_runner" {
  role_id     = "automaintainerSandboxRunner"
  title       = "AutoMaintainer sandbox runner"
  description = "Run the verification job with overrides, read and cancel its executions."
  permissions = [
    "run.executions.cancel",
    "run.executions.get",
    "run.jobs.get",
    "run.jobs.run",
    "run.jobs.runWithOverrides",
    "run.operations.get",
  ]
  depends_on = [google_project_service.services]
}

resource "google_cloud_run_v2_job_iam_member" "worker_runs_sandbox" {
  name     = google_cloud_run_v2_job.sandbox.name
  location = google_cloud_run_v2_job.sandbox.location
  role     = google_project_iam_custom_role.sandbox_runner.id
  member   = google_service_account.worker.member
}

# Upload copies, read results, delete both.
resource "google_storage_bucket_iam_member" "worker_sandbox_objects" {
  bucket = google_storage_bucket.sandbox.name
  role   = "roles/storage.objectUser"
  member = google_service_account.worker.member
}

# V4 signed URLs are signed with signBlob; the VM has no key. Granted on the
# worker's own account, which already holds its own tokens.
resource "google_service_account_iam_member" "worker_signs_urls" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.worker.member
}

# --- outputs ------------------------------------------------------------------------

output "sandbox_job" {
  description = "Cloud Run job that runs verification commands."
  value       = google_cloud_run_v2_job.sandbox.id
}

output "sandbox_bucket" {
  description = "Bucket for the per-execution working copies and results (1-day lifecycle)."
  value       = google_storage_bucket.sandbox.name
}

output "sandbox_service_account" {
  description = "Identity of the verification job; it holds no role."
  value       = google_service_account.sandbox.email
}
