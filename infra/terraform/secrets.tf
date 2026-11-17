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
