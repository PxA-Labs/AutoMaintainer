# Secret Manager containers, one per secret environment variable. Terraform
# never creates a version, so no secret value is ever in the state: the
# owner adds each value with `gcloud secrets versions add` (README.md).
# Access is granted per secret, only to the service that reads it.

resource "google_secret_manager_secret" "app" {
  for_each  = var.secrets
  secret_id = lower(replace(each.key, "_", "-"))

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_iam_member" "api" {
  for_each  = { for k, v in var.secrets : k => v if v.api }
  secret_id = google_secret_manager_secret.app[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.api.member
}

resource "google_secret_manager_secret_iam_member" "worker" {
  for_each  = { for k, v in var.secrets : k => v if v.worker }
  secret_id = google_secret_manager_secret.app[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.worker.member
}

# Redis AUTH (#532): the broker URL with its password,
# redis://:<password>@<host>:<port>/0, read by the API, the worker and (to
# start the self-hosted Redis with requirepass) the worker VM. A container
# only, like the secrets above: a password generated here (random_password
# plus a version) would be stored in the state in clear text. The owner
# adds the version (docs/DEPLOYMENT.md, Option D).
resource "google_secret_manager_secret" "redis_url" {
  count     = var.redis_auth_enabled ? 1 : 0
  secret_id = "redis-url"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_iam_member" "redis_url_api" {
  count     = var.redis_auth_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.redis_url[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.api.member
}

resource "google_secret_manager_secret_iam_member" "redis_url_worker" {
  count     = var.redis_auth_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.redis_url[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.worker.member
}
