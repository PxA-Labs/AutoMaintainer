# One service account per role, each with only what it uses.
#
#   api       Cloud Run runtime: reads its secrets; writes traces if enabled.
#   worker    VM runtime: reads its secrets, pulls the image, writes logs and
#             metrics; calls Vertex AI when rag_enabled.
#   deployer  GitHub Actions through WIF: pushes images, deploys new API
#             revisions and sets the worker's image metadata. It cannot read
#             secrets, change IAM or touch any other resource.

resource "google_service_account" "api" {
  account_id   = "${var.name_prefix}-api"
  display_name = "AutoMaintainer API (Cloud Run)"
  depends_on   = [google_project_service.services]
}

resource "google_service_account" "worker" {
  account_id   = "${var.name_prefix}-worker"
  display_name = "AutoMaintainer Celery worker (Compute Engine)"
  depends_on   = [google_project_service.services]
}

resource "google_service_account" "deployer" {
  account_id   = "${var.name_prefix}-deployer"
  display_name = "AutoMaintainer deployer (GitHub Actions)"
  depends_on   = [google_project_service.services]
}

# --- api ------------------------------------------------------------------------

resource "google_project_iam_member" "api_trace" {
  count   = var.cloud_trace_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = google_service_account.api.member
}

# --- worker ---------------------------------------------------------------------

resource "google_project_iam_member" "worker" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.worker.member
}

# Code retrieval (#526): the worker embeds chunks (indexing) and queries
# (PM prompt, Reviewer search_code) with Vertex AI. The API never does: it
# only enqueues runs, so it gets no Vertex AI role.
resource "google_project_iam_member" "worker_aiplatform" {
  count   = var.rag_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = google_service_account.worker.member
}

resource "google_artifact_registry_repository_iam_member" "worker_pull" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.worker.member
}

# --- deployer ---------------------------------------------------------------------

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

resource "google_cloud_run_v2_service_iam_member" "deployer" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

# A new revision runs as the api account, and setting metadata on a VM acts
# as the worker account, so the deployer must be allowed to act as both.
resource "google_service_account_iam_member" "deployer_acts_as" {
  for_each = {
    api    = google_service_account.api.name
    worker = google_service_account.worker.name
  }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

# Rolling the worker = changing one metadata key on one instance. A custom
# role keeps the deployer from stopping, deleting or SSH-ing into it, which
# roles/compute.instanceAdmin.v1 would allow. setMetadata cannot be limited
# to one key: the deployer can also rewrite user-data, startup-script or
# enable-oslogin (effective at the next boot). That is about the same power
# it already has by choosing the image the VM runs.
resource "google_project_iam_custom_role" "worker_roller" {
  role_id     = "automaintainerWorkerRoller"
  title       = "AutoMaintainer worker roller"
  description = "Read the worker VM and set its metadata (deploy workflow)."
  permissions = ["compute.instances.get", "compute.instances.setMetadata"]
  depends_on  = [google_project_service.services]
}

resource "google_compute_instance_iam_member" "deployer_roll_worker" {
  zone          = var.zone
  instance_name = module.worker.instance_name
  role          = google_project_iam_custom_role.worker_roller.id
  member        = google_service_account.deployer.member
}

# gcloud waits for the metadata operation; operations are project-level.
resource "google_project_iam_custom_role" "operation_reader" {
  role_id     = "automaintainerZoneOperationReader"
  title       = "AutoMaintainer zone operation reader"
  description = "Poll zonal Compute Engine operations (deploy workflow)."
  permissions = ["compute.zoneOperations.get"]
  depends_on  = [google_project_service.services]
}

resource "google_project_iam_member" "deployer_operations" {
  project = var.project_id
  role    = google_project_iam_custom_role.operation_reader.id
  member  = google_service_account.deployer.member
}
