# AutoMaintainer on Google Cloud (Terraform)

This directory describes the production deployment on Google Cloud (#527):
the API on Cloud Run, the Celery worker and beat on one Compute Engine VM,
Redis on that VM or in Memorystore, Secret Manager, Artifact Registry, the
verification sandbox, monitoring and a billing budget. Supabase stays the
database.

This file describes the configuration. The owner's runbook (first apply,
secret values, first deploy, optional features) and the cost estimate are in
[docs/DEPLOYMENT.md, Option D](../../docs/DEPLOYMENT.md#option-d-google-cloud-terraform).

- [Layout](#layout)
- [State](#state)
- [Variables that matter](#variables-that-matter)
- [Optional switches](#optional-switches)
- [Design notes](#design-notes)
- [Least privilege](#least-privilege)
- [Checks and CI](#checks-and-ci)
- [Cost](#cost)
- [Owner steps](#owner-steps)
- [Caveats](#caveats)

## Layout

| File | Contents |
|---|---|
| `versions.tf` | Terraform >= 1.9 and `hashicorp/google` `~> 8.0` (exact version locked in `.terraform.lock.hcl`) |
| `providers.tf` | Provider settings and the default labels `app = automaintainer`, `managed-by = terraform` |
| `variables.tf` | Project, GitHub, budget, network, API, Cloud Trace and Cloud Monitoring switches, code retrieval, worker, Redis, `app_env` and `secrets` |
| `main.tf` | Project APIs, Artifact Registry (keeps the 20 newest images, deletes older ones after 30 days), the network module, optional Memorystore, the worker and WIF modules, and `local.app_env`, the environment shared by the API and the worker |
| `run.tf` | Cloud Run service for the API and its public invoker binding |
| `iam.tf` | The `api`, `worker` and `deployer` service accounts and their roles, and the deployer's two custom roles |
| `secrets.tf` | Secret Manager containers (no versions), per-secret access, and the `redis-url` secret |
| `sandbox.tf` | Verification sandbox: the Cloud Run job, its own VPC, DNS zone and firewall, its bucket, its service account, the worker's custom runner role, and the variables and outputs that belong to it |
| `monitoring.tf` | Metric writer role for the API, the alert e-mail channel, five alert policies, the operations dashboard, and their variables and outputs |
| `budget.tf` | Billing budget and its optional e-mail channels |
| `outputs.tf` | `api_url`, `image_repository`, `wif_provider`, `deployer_service_account`, `github_repository_variables`, `secret_ids`, `redis_mode`, `redis_address`, `redis_url_secret_id` |
| `modules/network` | VPC, subnet with Private Google Access, Cloud Router and Cloud NAT |
| `modules/worker` | The worker VM, its fixed internal IP, firewall rules, `cloud-init.yaml.tftpl`, and the scripts it installs: `run-worker.sh`, `watch-image.sh`, `run-redis.sh` and `secrets_exec.py` |
| `modules/github_wif` | Workload identity pool, GitHub OIDC provider, and the deployer's `workloadIdentityUser` binding |
| `backend.tf.example` | Partial GCS backend; copy to `backend.tf` (git-ignored) to use remote state |
| `terraform.tfvars.example` | Example values, without secrets; copy to `terraform.tfvars` (git-ignored) |

## State

Without a `backend.tf`, state is local (`terraform.tfstate`, git-ignored).
The production state lives in a GCS bucket the owner creates once. The
bucket name is passed at init (`-backend-config="bucket=..."`) and never
committed; `backend.tf.example` sets the prefix `automaintainer/prod`.

The state holds no secret values: Terraform creates only secret containers,
and the owner adds the values. It does hold resource names, internal IPs and
the API URL. One exception: with `redis_mode = "memorystore"` and
`redis_auth_enabled`, the provider records Memorystore's generated AUTH
string in the state (attribute `auth_string`). No setting avoids it, so keep
the state bucket readable by the owner only.

## Variables that matter

Defaults are in `variables.tf`, `sandbox.tf` and `monitoring.tf`.

| Variable | Default | Notes |
|---|---|---|
| `project_id`, `region`, `zone` | `automaintainer-prod`, `us-central1`, `us-central1-a` | `zone` must be in `region` |
| `billing_account_id` | none (required) | For the budget |
| `monitoring_alert_email` | none (required) | An address, or `null` for no alert e-mail channel |
| `budget_amount`, `budget_alert_thresholds` | `100`, `[0.5, 0.9, 1.0]` | Alerts on actual spend; a fixed rule also alerts at 100 % of forecast spend |
| `mount_secrets` | `false` | Mount the secrets into the API. `false` for the first apply, `true` once every mounted secret has a version |
| `app_env` | `{}` | Non-secret environment for the API and the worker (both get the same values). Some names are refused or overridden; see [Design notes](#design-notes) |
| `secrets` | six entries | Which secret each service reads; see [Least privilege](#least-privilege) |
| `redis_mode` | `vm` | `vm` or `memorystore` |
| `redis_auth_enabled` | `true` | Redis requires a password from the `redis-url` secret |
| `api_max_instances`, `api_concurrency` | `3`, `40` | The minimum is fixed at 1 instance |
| `api_cpu`, `api_memory`, `api_request_timeout_seconds` | `1`, `1Gi`, `3600` | The timeout also caps the life of a WebSocket |
| `worker_machine_type`, `worker_disk_size_gb`, `worker_concurrency` | `e2-medium`, `30`, `2` | |
| `worker_stop_timeout_seconds` | `300` | Time running tasks get when the worker restarts |
| `subnet_cidr`, `sandbox_subnet_cidr` | `10.10.0.0/24`, `10.20.0.0/26` | Keep both at /26 or larger |
| `sandbox_image` | Google's sample job image | Has no `python3`; set it to the app image by digest after the first deploy |
| `sandbox_cpu`, `sandbox_memory`, `sandbox_task_timeout_seconds` | `1`, `2Gi`, `1800` | Per verification task |
| `enable_iap_ssh` | `true` | SSH to the worker through Identity-Aware Proxy |
| `deletion_protection` | `true` | Guards the Cloud Run service, the sandbox job and the worker VM |
| `github_repository`, `github_repository_id`, `github_repository_owner_id`, `github_environment` | `PxA-Labs/AutoMaintainer`, its numeric ids, `production` | Checked by the WIF provider |

## Optional switches

Every switch below is off by default except `redis_auth_enabled`.

| Switch | What it creates or changes |
|---|---|
| `cloud_trace_enabled` | Grants the API account `roles/cloudtrace.agent` and sets `OTEL_CLOUD_TRACE_ENABLED=true` on the API. The worker does not export traces |
| `cloud_monitoring_enabled` | Sets `OTEL_CLOUD_MONITORING_ENABLED=true` on the API and the worker. The metric writer role, the 5xx alert and the dashboard exist without it |
| `monitoring_app_alerts_enabled` | Creates the four alert policies on the backend's own metrics (run failure rate, p95 run duration, estimated LLM cost, worker heartbeat). Turn it on only after each metric has been written once |
| `rag_enabled` | Enables `aiplatform.googleapis.com`, grants the worker account `roles/aiplatform.user`, sets `RAG_INDEXING_ENABLED=true`, `RAG_RETRIEVAL_ENABLED=true` and `GOOGLE_CLOUD_LOCATION=<rag_location>`, and changes the `IMAGE_EXTRAS` entry of `github_repository_variables` to `server,rag` |
| `redis_mode = "memorystore"` | Enables `redis.googleapis.com` and creates a basic-tier Memorystore instance (Redis 7.2, `noeviction`, `memorystore_size_gb`) instead of the Redis container on the VM |
| `redis_auth_enabled` (default `true`) | Creates the `redis-url` secret container and its access for both accounts; the API and worker read `REDIS_URL` from it; the VM's Redis starts with `requirepass`; Memorystore gets `auth_enabled` |
| `enable_iap_ssh` (default `true`) | Creates the firewall rule for SSH from Identity-Aware Proxy |

The verification sandbox in `sandbox.tf` has no switch: it is always
created, and the worker always uses it. Until `sandbox_image` points at an
image with `python3`, every verification reports an error (fail closed).

## Design notes

**Image ownership.** Terraform creates the Cloud Run service with Google's
public placeholder image and the VM with an empty `automaintainer-image`
metadata key, and ignores both afterwards.
`.github/workflows/deploy-gcp.yml` owns the image: it deploys a new Cloud
Run revision by digest and sets the metadata key. Every other setting stays
here, and an apply keeps the image that is live.

**First apply without secret values.** Cloud Run refuses a revision that
mounts a secret without a version. `mount_secrets = false` lets the first
apply succeed before the owner has added any value. Set it to `true` after
the values exist. The worker reads its secrets only when its container
starts, which is after the first deploy.

**Shared environment.** `local.app_env` (`main.tf`) is `var.app_env` plus
`ENVIRONMENT=production`, `GOOGLE_CLOUD_PROJECT=<project_id>`,
`OTEL_CLOUD_MONITORING_ENABLED` and, with `rag_enabled`, the three RAG
variables. The API adds `OTEL_CLOUD_TRACE_ENABLED` and, until the
`redis-url` secret is mounted, a `REDIS_URL` without password. The worker
module adds `SANDBOX_BACKEND=cloud_run_job` and
`CLOUD_RUN_SANDBOX_{PROJECT,REGION,JOB,BUCKET,SIGNER}` over `app_env`, so
those cannot be changed or turned off from `app_env`.

**Refused `app_env` names.** Validation rejects `AUTH_DEV_BYPASS`,
`ENABLE_TERMINAL` and `VERIFY_ALLOW_LOCAL` (unsafe in a deployment);
`ENVIRONMENT`, `REDIS_URL` and `PORT` (set by Terraform or Cloud Run);
`OTEL_CLOUD_MONITORING_ENABLED` (use `cloud_monitoring_enabled`);
`RAG_INDEXING_ENABLED`, `RAG_RETRIEVAL_ENABLED` and `GOOGLE_CLOUD_LOCATION`
(use `rag_enabled` and `rag_location`); any name that is also in
`var.secrets`; and multi-line values (the worker passes `app_env` through a
`docker run` env file). `GOOGLE_CLOUD_PROJECT` and `OTEL_CLOUD_TRACE_ENABLED`
are not refused but are overridden by Terraform.

**Worker VM.** Container-Optimized OS, shielded VM, OS Login, no public IP,
outbound through Cloud NAT. cloud-init, re-applied on every boot because
`/etc` is not persistent on COS, installs three systemd units:

- `automaintainer-worker`: `run-worker.sh` reads the image name from the
  metadata server, pulls it from Artifact Registry with
  `docker-credential-gcr`, and runs
  `celery -A celery_app worker -Q agent_runs,maintenance,github_sync,default -B`
  through `secrets_exec.py`, with the clones in the
  `automaintainer-workspaces` volume. systemd restarts it 30 seconds after
  any exit. Stopping it gives running tasks `worker_stop_timeout_seconds`;
  Celery requeues the rest (`acks_late`).
- `automaintainer-image-watch`: polls the metadata key every 20 seconds and
  restarts the worker when it changes.
- `automaintainer-redis` (`redis_mode = "vm"` only): Redis 7.4, pinned by tag
  and digest, with `appendonly yes` and `noeviction`, opened in the host
  firewall to the subnet only. With `redis_auth_enabled`, `run-redis.sh`
  reads the `redis-url` secret with the VM's account and passes the password
  to Redis as `requirepass` on stdin. It refuses a password shorter than 32
  characters or with characters outside `A-Z a-z 0-9 . _ ~ -`.

**Secrets on the VM.** `secrets_exec.py` (standard library only, run with
the image's Python) reads each secret's latest version with the VM service
account's token and replaces itself with the Celery command. Values exist
only in that process's environment, as on Cloud Run: not on disk, not on the
`docker run` command line, and multi-line values such as the App's PEM key
stay intact. A missing version stops the start (the unit retries) and names
the variable, never the value. For `REDIS_URL` it also refuses a value that
is not a `redis://` or `rediss://` URL with a password.
`backend/tests/test_gcp_worker_secrets_exec.py` covers it.

**Redis AUTH.** With `redis_auth_enabled` (default `true`), `secrets.tf`
creates the container `redis-url` with no version. The owner adds the
version, `redis://:<password>@<redis_address>/0` (`terraform output
redis_address`). The API mounts it as `REDIS_URL` once `mount_secrets` is
`true`. The worker module puts it in `AUTOMAINTAINER_SECRETS` instead of a
plain `REDIS_URL`. Memorystore gets `auth_enabled`, and Google generates its
AUTH string (see [State](#state)). Redis traffic is not encrypted in either
mode, so the password crosses the VPC in clear.

**Network.** One regional subnet holds the VM and the IPs Cloud Run's Direct
VPC egress assigns to API instances, so keep it at /26 or larger. The API
sends only private ranges through the VPC (`PRIVATE_RANGES_ONLY`). Ingress to
the VM is limited to Redis from the subnet (`vm` mode) and, with
`enable_iap_ssh`, SSH from Identity-Aware Proxy.

**Verification sandbox.** `sandbox.tf` runs each verification command as one
execution of the Cloud Run job `<name_prefix>-sandbox`, as a service account
with no role. The job's egress goes into its own VPC, which has no default
route, no NAT, no peering, and a deny-all egress firewall except TCP 443 to
Private Google Access (199.36.153.8/30). A private DNS zone resolves
`storage.googleapis.com` to that range. The bucket
`<project>-<name_prefix>-sandbox` has uniform access, public access
prevention, no soft delete and a 1-day lifecycle delete. The threat model is
in [docs/VERIFICATION.md](../../docs/VERIFICATION.md#cloud-run-jobs-sandbox-backend).

**Who can deploy.** The WIF provider (`modules/github_wif`) accepts a GitHub
OIDC token only if all of these hold, checked by Google:

- `repository` is `PxA-Labs/AutoMaintainer` and `repository_id` and
  `repository_owner_id` are its numeric ids (`github_repository_id`,
  `github_repository_owner_id`), so a renamed or re-created repository
  cannot take the name over;
- `environment` is `production`;
- `ref` is `refs/heads/main` or starts with `refs/tags/v`.

The `workloadIdentityUser` binding is on the repository id. A fork, a pull
request, a job outside the environment or a run on any other branch cannot
impersonate the deployer, whatever the environment's settings on GitHub.
Two gates remain on GitHub's side: the environment's required reviewers, and
who may push `v*` tags (anyone with write access, unless a tag ruleset
restricts it). A workflow file changed on `main` or in a tagged commit runs
with deployer credentials, as in any deploy pipeline.

## Least privilege

One service account per role, each with only what it uses.

| Account | Roles |
|---|---|
| `<prefix>-api` | `secretmanager.secretAccessor` on each secret with `api = true` and on `redis-url`; `monitoring.metricWriter`; `cloudtrace.agent` with `cloud_trace_enabled` |
| `<prefix>-worker` | `secretmanager.secretAccessor` on each secret with `worker = true` and on `redis-url`; `logging.logWriter`; `monitoring.metricWriter`; `artifactregistry.reader` on the one repository; `aiplatform.user` with `rag_enabled`; the custom `automaintainerSandboxRunner` role on the sandbox job; `storage.objectUser` on the sandbox bucket; `iam.serviceAccountTokenCreator` on itself (to sign the sandbox URLs) |
| `<prefix>-deployer` | `artifactregistry.writer` on the one repository; `run.developer` on the one Cloud Run service; `iam.serviceAccountUser` on the `api` and `worker` accounts; the custom `automaintainerWorkerRoller` role (`compute.instances.get`, `compute.instances.setMetadata`) on the one VM; the custom `automaintainerZoneOperationReader` role (`compute.zoneOperations.get`) on the project |
| `<prefix>-sandbox` | none |

Secret access by default (`var.secrets`): the API and the worker read
`SUPABASE_SERVICE_KEY`, `GROQ_API_KEY`, `GITHUB_APP_PRIVATE_KEY` and
`GITHUB_WEBHOOK_SECRET`; only the API reads `GITHUB_APP_CLIENT_SECRET`;
nobody reads `GITHUB_TOKEN` unless PAT mode is chosen by setting its `api`
and `worker` flags.

The deployer cannot read secrets or change IAM. It must act as the two
runtime accounts to deploy a revision that runs as `api` and to change the
metadata of a VM that runs as `worker`. `setMetadata` cannot be limited to
one key, so the deployer can also rewrite the VM's `user-data`,
`startup-script` or `enable-oslogin` (effective at the next boot). That is
about the same power it already has by choosing the image.

`roles/run.developer` on the service alone, with no project-level Run role,
has not yet been exercised by a real deploy. If `gcloud run deploy` is
denied, the error names the missing permission.

## Checks and CI

```bash
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

`.github/workflows/ci-terraform.yml` runs these on pushes and pull requests
that touch `infra/terraform/**`, with Terraform 1.17.1 and
`-lockfile=readonly`. No CI job holds Google credentials or runs `plan` or
`apply`; the owner runs both locally. These tests pin the configuration:

- `backend/tests/test_gcp_deploy_config.py`: layout and state, secrets,
  Cloud Run and VM settings, WIF, the refused `app_env` names, the budget,
  the deploy workflow and the `rag_enabled` wiring;
- `backend/tests/test_gcp_monitoring_config.py`: every alert and chart
  filter against the metric catalogue in `backend/observability.py`;
- `backend/tests/test_gcp_sandbox_config.py`: the sandbox network, bucket,
  job and roles;
- `backend/tests/test_gcp_redis_auth.py`: Redis AUTH, including
  `run-redis.sh`;
- `backend/tests/test_gcp_worker_secrets_exec.py`: `secrets_exec.py`.

## Cost

The estimate for the default settings is in
[docs/DEPLOYMENT.md, Estimated monthly cost](../../docs/DEPLOYMENT.md#estimated-monthly-cost):
about USD 40-60 a month with `redis_mode = "vm"` and about USD 75-95 with
`memorystore`, at list prices that were not checked against the current
price list. The budget (`budget_amount`, 100 by default) sends alerts but
never stops spending.

## Owner steps

The ordered runbook is in
[docs/DEPLOYMENT.md, Option D](../../docs/DEPLOYMENT.md#option-d-google-cloud-terraform):
state bucket, default compute service account, `v*` tag ruleset, apply,
secret values, repository variables, first deploy, then the sandbox image
and smoke test and the optional features.

## Caveats

- Custom IAM roles (`automaintainerWorkerRoller`,
  `automaintainerZoneOperationReader`, `automaintainerSandboxRunner`) are
  soft-deleted on destroy, and their ids stay reserved for weeks. An
  apply soon after a destroy in the same project fails on them. Undelete
  them (`gcloud iam roles undelete`) or change the `role_id`s.
- The VM boots from the `cos-stable` image family, resolved once at
  creation. The boot disk ignores later images, so a new COS release never
  recreates the VM; recreate it deliberately to move to one. The Redis image
  is pinned by tag and digest; bump both together. The Cloud Run placeholder
  image is unpinned; it runs only until the first deploy and is ignored
  afterwards.
- The worker container uses the host network and can reach the metadata
  server, so code running in it can obtain the worker service account's
  token and read the worker's secrets. This is why verification runs in the
  sandbox job and `VERIFY_ALLOW_LOCAL` is refused.
- Private Google Access in the sandbox VPC reaches every `*.googleapis.com`
  API, not only the bucket. VPC Service Controls would close that path; they
  are not set up here.
- `deletion_protection` (default `true`) guards the Cloud Run service, the
  sandbox job and the VM; set it to `false` and apply before a deliberate
  destroy.
- Changing `app_env`, `var.secrets`, the Redis mode or a switch that changes
  the worker's environment updates the VM's metadata in place, but the VM
  reads it only at boot: reset the VM after such a change.
