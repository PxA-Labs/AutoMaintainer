provider "google" {
  project = var.project_id
  region  = var.region

  # The Billing Budgets API needs a quota project when it is called with user
  # credentials (application default credentials of the owner).
  billing_project       = var.project_id
  user_project_override = true

  default_labels = {
    app        = "automaintainer"
    managed-by = "terraform"
  }
}
