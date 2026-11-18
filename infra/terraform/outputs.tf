output "api_url" {
  description = "Public URL of the API. Point the GitHub App webhook at <api_url>/webhooks/github."
  value       = google_cloud_run_v2_service.api.uri
}

output "image_repository" {
  description = "Image name (without tag) the deploy workflow pushes: repository variable GCP_IMAGE_REPOSITORY."
  value       = local.image_repository
}

output "wif_provider" {
  description = "Workload identity provider: repository variable GCP_WORKLOAD_IDENTITY_PROVIDER."
  value       = module.github_wif.provider_name
}

output "deployer_service_account" {
  description = "Service account the deploy workflow impersonates: repository variable GCP_DEPLOYER_SERVICE_ACCOUNT."
  value       = google_service_account.deployer.email
}

output "github_repository_variables" {
  description = "Every repository variable .github/workflows/deploy-gcp.yml reads (none is secret)."
  value = {
    GCP_PROJECT_ID                 = var.project_id
    GCP_REGION                     = var.region
    GCP_WORKLOAD_IDENTITY_PROVIDER = module.github_wif.provider_name
    GCP_DEPLOYER_SERVICE_ACCOUNT   = google_service_account.deployer.email
    GCP_IMAGE_REPOSITORY           = local.image_repository
    GCP_RUN_SERVICE                = google_cloud_run_v2_service.api.name
    GCP_WORKER_INSTANCE            = module.worker.instance_name
    GCP_WORKER_ZONE                = var.zone
  }
}

output "secret_ids" {
  description = "Secret Manager secret per environment variable; add a version to each mounted one."
  value       = { for k, s in google_secret_manager_secret.app : k => s.secret_id }
}

output "redis_mode" {
  value = var.redis_mode
}
