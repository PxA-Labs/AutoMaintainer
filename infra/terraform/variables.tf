# --- Project ------------------------------------------------------------------

variable "project_id" {
  description = "Google Cloud project that hosts AutoMaintainer."
  type        = string
  default     = "automaintainer-prod"
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry, the subnet and Memorystore."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone of the worker VM; must be in var.region."
  type        = string
  default     = "us-central1-a"

  validation {
    condition     = startswith(var.zone, "${var.region}-")
    error_message = "zone must be a zone of var.region."
  }
}

variable "name_prefix" {
  description = "Prefix of every resource name."
  type        = string
  default     = "automaintainer"

  validation {
    # Keeps "<prefix>-deployer" inside the 30-character service account limit.
    condition     = can(regex("^[a-z][a-z0-9-]{2,19}$", var.name_prefix))
    error_message = "name_prefix must be 3-20 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "deletion_protection" {
  description = "Refuse to destroy the Cloud Run service and the worker VM."
  type        = bool
  default     = true
}

# --- GitHub -------------------------------------------------------------------

variable "github_repository" {
  description = "The only GitHub repository (owner/name) whose Actions may deploy."
  type        = string
  default     = "PxA-Labs/AutoMaintainer"
}

variable "github_repository_id" {
  description = "Numeric id of github_repository (`gh api repos/OWNER/NAME --jq .id`). The WIF provider checks it as well as the name, so a renamed or re-created repository cannot take the name over."
  type        = string
  default     = "1254396219"

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be the numeric repository id."
  }
}

variable "github_repository_owner_id" {
  description = "Numeric id of the repository owner (`gh api repos/OWNER/NAME --jq .owner.id`)."
  type        = string
  default     = "284408399"

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_owner_id))
    error_message = "github_repository_owner_id must be the numeric owner id."
  }
}

variable "github_environment" {
  description = "GitHub Actions environment the deploy job runs in. Only OIDC tokens issued for this environment (and for main or a v* tag) are accepted; its required reviewers gate every deploy."
  type        = string
  default     = "production"
}

# --- Budget -------------------------------------------------------------------

variable "billing_account_id" {
  description = "Billing account of the project (XXXXXX-XXXXXX-XXXXXX), for the budget. `gcloud billing projects describe <project>` prints it."
  type        = string

  validation {
    condition     = can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account_id))
    error_message = "billing_account_id must look like 012345-6789AB-CDEF01."
  }
}

variable "budget_amount" {
  description = "Monthly budget, in whole units of budget_currency."
  type        = number
  default     = 100
}

variable "budget_currency" {
  description = "Currency of the budget. Must be the billing account's currency."
  type        = string
  default     = "USD"
}

variable "budget_alert_thresholds" {
  description = "Fractions of the budget (actual spend) that send an alert."
  type        = list(number)
  default     = [0.5, 0.9, 1.0]
}

variable "budget_notification_emails" {
  description = "Extra e-mail recipients of budget alerts. Billing account administrators are always notified."
  type        = list(string)
  default     = []
}

# --- Network ------------------------------------------------------------------

variable "subnet_cidr" {
  description = "Range of the subnet shared by the worker VM and Cloud Run's Direct VPC egress (one IP per Cloud Run instance, so keep it at /26 or larger)."
  type        = string
  default     = "10.10.0.0/24"
}

variable "enable_iap_ssh" {
  description = "Allow SSH to the worker VM through Identity-Aware Proxy (OS Login and IAM still decide who may connect)."
  type        = bool
  default     = true
}

# --- API (Cloud Run) ------------------------------------------------------------

variable "api_max_instances" {
  description = "Maximum Cloud Run instances of the API. The minimum is fixed at 1 so GitHub webhooks never wait for a cold start."
  type        = number
  default     = 3

  validation {
    condition     = var.api_max_instances >= 1
    error_message = "api_max_instances must be at least 1."
  }
}

variable "api_concurrency" {
  description = "Concurrent requests per API instance."
  type        = number
  default     = 40
}

variable "api_request_timeout_seconds" {
  description = "Cloud Run request timeout. It also caps the life of a WebSocket (run logs, terminal), hence the maximum by default."
  type        = number
  default     = 3600

  validation {
    condition     = var.api_request_timeout_seconds >= 1 && var.api_request_timeout_seconds <= 3600
    error_message = "api_request_timeout_seconds must be between 1 and 3600."
  }
}

variable "api_cpu" {
  description = "vCPUs per API instance."
  type        = string
  default     = "1"
}

variable "api_memory" {
  description = "Memory per API instance."
  type        = string
  default     = "1Gi"
}

variable "mount_secrets" {
  description = "Mount the Secret Manager secrets into the API. Leave false for the first apply (the secrets have no versions yet and Cloud Run refuses a revision that names a missing one); set true once every mounted secret has a version (README.md, step 4)."
  type        = bool
  default     = false
}

variable "cloud_trace_enabled" {
  description = "Export the API's OpenTelemetry traces to Cloud Trace (OTEL_CLOUD_TRACE_ENABLED) and grant the API roles/cloudtrace.agent."
  type        = bool
  default     = false
}

variable "cloud_monitoring_enabled" {
  description = "Export the API's and the worker's OpenTelemetry metrics to Cloud Monitoring (OTEL_CLOUD_MONITORING_ENABLED). Both accounts already hold roles/monitoring.metricWriter (monitoring.tf, iam.tf). The application alerts (monitoring_app_alerts_enabled) need it on."
  type        = bool
  default     = false
}

# --- Worker (Compute Engine) --------------------------------------------------

variable "worker_machine_type" {
  description = "Machine type of the worker VM, which runs the Celery worker and beat (and Redis when redis_mode is \"vm\")."
  type        = string
  default     = "e2-medium"
}

variable "worker_disk_size_gb" {
  description = "Boot disk of the worker VM; holds the image, the repository clones and the Redis append-only file."
  type        = number
  default     = 30
}

variable "worker_concurrency" {
  description = "Celery worker processes on the VM (parallel agent runs)."
  type        = number
  default     = 2
}

variable "worker_stop_timeout_seconds" {
  description = "How long a worker restart waits for running tasks before killing them (Celery requeues them: acks_late)."
  type        = number
  default     = 300
}

# --- Redis ------------------------------------------------------------------------

variable "redis_mode" {
  description = <<-EOT
    Where the Celery broker runs.
    "vm" (default, cheapest): a Redis container on the worker VM, reachable
    from Cloud Run over the VPC. No extra cost, but it shares the VM's
    memory and disk, has no replica, and is unavailable while the VM restarts.
    "memorystore": a Memorystore for Redis basic-tier instance peered with
    the private VPC. Managed, survives worker restarts, costs more than the
    worker VM itself (see docs/DEPLOYMENT.md). Basic tier has no replica
    either.
  EOT
  type        = string
  default     = "vm"

  validation {
    condition     = contains(["vm", "memorystore"], var.redis_mode)
    error_message = "redis_mode must be \"vm\" or \"memorystore\"."
  }
}

variable "redis_auth_enabled" {
  description = <<-EOT
    Require a password on Redis (#532). Terraform creates the empty secret
    "redis-url"; the owner adds the broker URL with the password as its
    version (docs/DEPLOYMENT.md, Option D), so the password is never in the
    Terraform state. The API (once mount_secrets is true) and the worker read
    REDIS_URL from it, and the self-hosted Redis starts with requirepass.
    With "memorystore", the instance gets AUTH and Google generates the
    password (its AUTH string), which the owner puts into the same URL.
    Until the version exists, the worker and the self-hosted Redis do not
    start.
  EOT
  type        = bool
  default     = true
}

variable "redis_vm_maxmemory" {
  description = "maxmemory of the Redis container (redis_mode = \"vm\"). With noeviction, writes fail beyond it instead of the VM running out of memory."
  type        = string
  default     = "512mb"
}

variable "memorystore_size_gb" {
  description = "Memorystore capacity (redis_mode = \"memorystore\")."
  type        = number
  default     = 1
}

# --- Application configuration ---------------------------------------------

variable "app_env" {
  description = <<-EOT
    Non-secret environment for the API and the worker, e.g. SUPABASE_URL,
    GITHUB_APP_ID, GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID, ALLOWED_ORIGINS,
    DASHBOARD_URL, MERGE_POLICY (docs/DEPLOYMENT.md lists them). Both get the
    same values, so MERGE_POLICY matches as DEPLOYMENT.md requires. ENVIRONMENT,
    REDIS_URL and OTEL_CLOUD_MONITORING_ENABLED (cloud_monitoring_enabled) are
    set by this configuration. Secrets go in var.secrets.
    Refused: AUTH_DEV_BYPASS (never in a deployment), ENABLE_TERMINAL (the
    API is public) and VERIFY_ALLOW_LOCAL (runs untrusted repository code on
    the worker; stays off until sandboxed verification lands, #527).
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k in ["AUTH_DEV_BYPASS", "ENABLE_TERMINAL", "VERIFY_ALLOW_LOCAL", "ENVIRONMENT", "REDIS_URL", "PORT"] :
      !contains(keys(var.app_env), k)
    ])
    error_message = "app_env must not set AUTH_DEV_BYPASS, ENABLE_TERMINAL, VERIFY_ALLOW_LOCAL, ENVIRONMENT, REDIS_URL or PORT."
  }

  validation {
    # Set from cloud_monitoring_enabled, which would silently replace it.
    condition     = !contains(keys(var.app_env), "OTEL_CLOUD_MONITORING_ENABLED")
    error_message = "Set cloud_monitoring_enabled instead of OTEL_CLOUD_MONITORING_ENABLED in app_env."
  }

  validation {
    condition     = alltrue([for k in keys(var.app_env) : !contains(keys(var.secrets), k)])
    error_message = "A name in var.secrets cannot also be a plain value in app_env."
  }

  validation {
    # The worker passes app_env through `docker run --env-file`, which has
    # no multi-line values.
    condition     = alltrue([for v in values(var.app_env) : !strcontains(v, "\n")])
    error_message = "app_env values must be single-line."
  }
}

variable "secrets" {
  description = <<-EOT
    Secret Manager secrets, by the environment variable they become, and
    which service reads them. Terraform creates the secret containers only;
    the owner adds each value as a version (README.md, step 4), so no value
    is ever in the Terraform state. GITHUB_TOKEN is created but mounted
    nowhere: set api and worker to true only for single-tenant PAT mode.
    Extra Groq keys (GROQ_API_KEY_1 ...) can be added here.
  EOT
  type = map(object({
    api    = bool
    worker = bool
  }))
  default = {
    SUPABASE_SERVICE_KEY     = { api = true, worker = true }
    GROQ_API_KEY             = { api = true, worker = true }
    GITHUB_APP_PRIVATE_KEY   = { api = true, worker = true }
    GITHUB_WEBHOOK_SECRET    = { api = true, worker = true }
    GITHUB_APP_CLIENT_SECRET = { api = true, worker = false }
    GITHUB_TOKEN             = { api = false, worker = false }
  }

  validation {
    condition     = alltrue([for k in keys(var.secrets) : can(regex("^[A-Z][A-Z0-9_]*$", k))])
    error_message = "secret keys must be environment variable names (A-Z, 0-9, _)."
  }

  validation {
    # REDIS_URL has its own secret, "redis-url" (redis_auth_enabled).
    condition     = !contains(keys(var.secrets), "REDIS_URL")
    error_message = "REDIS_URL cannot be in var.secrets; redis_auth_enabled manages its secret."
  }
}
