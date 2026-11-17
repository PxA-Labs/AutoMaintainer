# Workload Identity Federation for GitHub Actions: the deploy workflow trades
# its OIDC token for short-lived credentials of the deployer service account.
# No service account key exists.
#
# The provider accepts only tokens for one repository AND one GitHub
# environment, so a workflow on another branch or a fork, or a job without
# the protected environment, cannot impersonate the deployer.

variable "pool_id" {
  description = "Workload identity pool id."
  type        = string
}

variable "github_repository" {
  description = "owner/name of the only repository allowed to authenticate."
  type        = string
}

variable "github_environment" {
  description = "GitHub environment the OIDC token must be issued for."
  type        = string
}

variable "service_account_name" {
  description = "Full resource name (projects/.../serviceAccounts/...) of the service account to impersonate."
  type        = string
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = var.pool_id
  display_name              = "GitHub Actions"
  description               = "Deploys from ${var.github_repository}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"         = "assertion.sub"
    "attribute.repository"   = "assertion.repository"
    "attribute.environment"  = "assertion.environment"
    "attribute.ref"          = "assertion.ref"
    "attribute.workflow_ref" = "assertion.workflow_ref"
  }

  attribute_condition = "assertion.repository == \"${var.github_repository}\" && assertion.environment == \"${var.github_environment}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = var.service_account_name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

output "provider_name" {
  description = "Value for google-github-actions/auth's workload_identity_provider."
  value       = google_iam_workload_identity_pool_provider.github.name
}
