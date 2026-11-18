# The API on Cloud Run. Public ingress and unauthenticated invocation:
# GitHub webhooks and the browser dashboard call it directly, and the app
# enforces authentication itself (Supabase JWTs, webhook signatures).
#
# The image is created with a public placeholder and then owned by the deploy
# workflow (`gcloud run deploy --image`), hence ignore_changes. Every other
# setting stays in Terraform; an apply keeps whatever image is live.

locals {
  placeholder_image = "us-docker.pkg.dev/cloudrun/container/hello"

  api_env = merge(local.app_env, {
    REDIS_URL                = local.redis_url
    OTEL_CLOUD_TRACE_ENABLED = var.cloud_trace_enabled ? "true" : "false"
  })
  api_secrets = { for k, v in var.secrets : k => v if v.api && var.mount_secrets }
}

resource "google_cloud_run_v2_service" "api" {
  name                = "${var.name_prefix}-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account                  = google_service_account.api.email
    max_instance_request_concurrency = var.api_concurrency
    timeout                          = "${var.api_request_timeout_seconds}s"
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      # One warm instance: GitHub gives a webhook delivery 10 seconds.
      min_instance_count = 1
      max_instance_count = var.api_max_instances
    }

    # Direct VPC egress (no connector) to reach Redis on the private network.
    # Only private ranges go through the VPC; GitHub, Groq and Supabase are
    # reached directly.
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = module.network.network_id
        subnetwork = module.network.subnet_id
      }
    }

    containers {
      image = local.placeholder_image

      # Cloud Run sets PORT to this; the image's CMD listens on $PORT.
      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.api_cpu
          memory = var.api_memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.api_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.api_secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.key].secret_id
              version = "latest"
            }
          }
        }
      }

      # /healthz is the liveness route of the image's HEALTHCHECK: it never
      # touches Supabase or Redis, so a dependency outage does not restart
      # the API.
      startup_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 5
        timeout_seconds   = 3
        failure_threshold = 24
      }

      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 30
        timeout_seconds   = 5
        failure_threshold = 3
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.services,
    google_secret_manager_secret_iam_member.api,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
