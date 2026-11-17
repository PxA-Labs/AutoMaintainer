# Workload Identity Federation for GitHub Actions: the deploy workflow trades
# its OIDC token for short-lived credentials of the deployer service account.
# No service account key exists.
#
# Google accepts a GitHub OIDC token only if all of these hold:
# - it was issued to this repository, matched by name AND by the numeric
#   repository and owner ids, so a renamed, transferred or deleted and
#   re-created repository cannot take over the name;
# - the job runs in the protected GitHub environment;
# - the ref is main or a release tag (refs/tags/v..., deploy-gcp.yml runs
#   for v*.*.* tags).
# So a fork, a job outside the environment, or a workflow run from any
# other branch (for example a modified deploy-gcp.yml dispatched on a
# feature branch by someone with write access) cannot impersonate the
# deployer, independently of the environment's branch policy on GitHub.
# A tag can be pushed by anyone with write access unless a tag ruleset
# protects the prefix; the environment's required reviewers remain the
# second gate (docs/DEPLOYMENT.md, Option D, step 4).

variable "pool_id" {
  description = "Workload identity pool id."
  type        = string
}

variable "github_repository" {
  description = "owner/name of the only repository allowed to authenticate."
  type        = string
}

variable "github_repository_id" {
  description = "Numeric id of github_repository (gh api repos/OWNER/NAME --jq .id)."
  type        = string
}

variable "github_repository_owner_id" {
  description = "Numeric id of the repository owner (gh api repos/OWNER/NAME --jq .owner.id)."
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
    "google.subject"          = "assertion.sub"
    "attribute.repository"    = "assertion.repository"
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.environment"   = "assertion.environment"
    "attribute.ref"           = "assertion.ref"
    "attribute.workflow_ref"  = "assertion.workflow_ref"
  }

  attribute_condition = join(" && ", [
    "assertion.repository == \"${var.github_repository}\"",
    "assertion.repository_id == \"${var.github_repository_id}\"",
    "assertion.repository_owner_id == \"${var.github_repository_owner_id}\"",
    "assertion.environment == \"${var.github_environment}\"",
    "(assertion.ref == \"refs/heads/main\" || assertion.ref.startsWith(\"refs/tags/v\"))",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = var.service_account_name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

output "provider_name" {
  description = "Value for google-github-actions/auth's workload_identity_provider."
  value       = google_iam_workload_identity_pool_provider.github.name
}
