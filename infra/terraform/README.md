# AutoMaintainer on Google Cloud (Terraform)

Infrastructure for the production deployment on Google Cloud (#527). The
owner's step-by-step guide, the trade-offs that affect operators and the
cost estimate are in [docs/DEPLOYMENT.md, Option D](../../docs/DEPLOYMENT.md#option-d-google-cloud-terraform).
This file describes the configuration itself.

## Layout

| File | Contents |
|---|---|
| `versions.tf`, `providers.tf` | Terraform >= 1.9, `hashicorp/google` 8.x (locked in `.terraform.lock.hcl`), default labels |
| `main.tf` | Project APIs, Artifact Registry, the network, optional Memorystore, the worker and WIF modules |
| `run.tf` | Cloud Run service for the API and its public invoker binding |
| `iam.tf` | The `api`, `worker` and `deployer` service accounts and their roles |
| `secrets.tf` | Secret Manager containers (no versions) and per-secret access |
| `budget.tf` | Billing budget and optional e-mail channels |
| `outputs.tf` | `api_url`, `image_repository`, `wif_provider`, `deployer_service_account`, and every repository variable the deploy workflow needs |
| `modules/network` | VPC, subnet with Private Google Access, Cloud Router and Cloud NAT |
| `modules/worker` | The worker VM, its fixed internal IP, firewall rules, cloud-init, the scripts it installs and `secrets_exec.py` |
| `modules/github_wif` | Workload identity pool and GitHub OIDC provider, and the deployer's `workloadIdentityUser` binding |
| `backend.tf.example` | Partial GCS backend; copy to `backend.tf` (git-ignored) to use remote state |
| `terraform.tfvars.example` | Example values, without secrets |

## State

With no `backend.tf`, state is local (`terraform.tfstate`, git-ignored). The
production state lives in a GCS bucket the owner creates once; the bucket
name is passed at init (`-backend-config="bucket=..."`), so it is never
committed. The state holds no secret values: Terraform creates only secret
containers. It does hold resource names, internal IPs and the API URL.

## Design notes

**Image ownership.** Terraform creates the Cloud Run service with Google's
public placeholder image and the VM with an empty `automaintainer-image`
metadata key, and ignores both afterwards. `.github/workflows/deploy-gcp.yml`
owns the image: it deploys a new Cloud Run revision by digest and sets the
metadata key. Every other setting stays here, and an apply keeps the image
that is live.

**First apply without secret values.** Cloud Run refuses a revision that
mounts a secret without a version. `mount_secrets` (default `false`) lets the
first apply succeed before the owner has added any value; set it to `true`
after step 3 of the guide. The worker reads its secrets only when its
container starts, which is after the first deploy.

**Worker VM.** Container-Optimized OS, shielded VM, OS Login, no public IP,
outbound through Cloud NAT. cloud-init (re-applied on every boot, since `/etc`
is not persistent on COS) installs three systemd units:

- `automaintainer-worker`: `run-worker.sh` reads the image name from the
  metadata server, pulls it from Artifact Registry with
  `docker-credential-gcr`, and runs
  `celery -A celery_app worker -Q agent_runs,maintenance,github_sync,default -B`
  through `secrets_exec.py`, with the clones in the `automaintainer-workspaces`
  volume. It is restarted 30 seconds after any exit. Stopping it gives
  running tasks `worker_stop_timeout_seconds`; Celery requeues the rest
  (`acks_late`).
- `automaintainer-image-watch`: polls the metadata key every 20 seconds and
  restarts the worker when it changes.
- `automaintainer-redis` (`redis_mode = "vm"` only): Redis 7.4 with
  `appendonly yes` and `noeviction`, opened in the host firewall to the subnet
  only.

**Secrets on the VM.** `secrets_exec.py` (standard library only, run with
the image's Python) reads each secret's latest version with the VM service
account's token and replaces itself with the Celery command. Values exist
only in that process's environment, as on Cloud Run: not on disk, not on the
`docker run` command line, and multi-line values such as the App's PEM key
stay intact. A missing version stops the start (the unit retries) and names
the variable, never the value. `backend/tests/test_gcp_worker_secrets_exec.py`
covers it.

**Network.** One regional subnet holds the VM and the IPs Cloud Run's Direct
VPC egress assigns to API instances, so keep it at /26 or larger. The API
sends only private ranges through the VPC (`PRIVATE_RANGES_ONLY`). Ingress to
the VM is limited to Redis from the subnet (vm mode) and, if
`enable_iap_ssh`, SSH from Identity-Aware Proxy.

**Least privilege.** Secret access is granted per secret to the service that
reads it (`GITHUB_APP_CLIENT_SECRET` only to the API, `GITHUB_TOKEN` to
nobody unless PAT mode is chosen). The deployer can push to the one
repository, act as the two runtime accounts (required to deploy a revision
that runs as `api` and to change the metadata of a VM that runs as
`worker`), deploy the one Cloud Run service, and get and set metadata on the
one VM through a custom role, plus read zone operations. `setMetadata`
cannot be limited to one key, so the deployer can also rewrite the VM's
`user-data`, `startup-script` or `enable-oslogin` (effective at the next
boot); that is about the code execution it already has by choosing the
image.

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
with deployer credentials, as any deploy pipeline does.

**Dangerous settings are refused.** `app_env` may not set
`AUTH_DEV_BYPASS`, `ENABLE_TERMINAL` or `VERIFY_ALLOW_LOCAL`, nor the values
Terraform owns (`ENVIRONMENT=production`, `REDIS_URL`, `PORT`).

## Monitoring

`monitoring.tf` holds the Cloud Monitoring side of the backend's metrics
(docs/DEPLOYMENT.md, "Metrics and alerts"): `roles/monitoring.metricWriter`
for the API account (the worker has it in `iam.tf`; the Monitoring API is
enabled in `main.tf`), an e-mail notification channel from
`monitoring_alert_email` (no default; `null` for none), five alert policies
and an operations dashboard. The backend exports only with
`OTEL_CLOUD_MONITORING_ENABLED=true` in `app_env`; nothing in `run.tf` or the
worker module changes.

- The API 5xx ratio alert uses Cloud Run's built-in `request_count` and
  exists from the first apply.
- The run failure rate, p95 run duration, estimated LLM cost per day and
  worker heartbeat alerts filter on `workload.googleapis.com/<metric>`.
  Cloud Monitoring refuses (404) a policy on a metric type that has never
  been written, so they are created only with
  `monitoring_app_alerts_enabled = true`, after the first runs have exported
  each metric. `backend/tests/test_gcp_monitoring_config.py` checks that each
  filter names a metric and labels from the catalogue in
  `backend/observability.py`.
- Every application series is per process (`generic_task`, one `task_id` per
  process, plus an `opentelemetry_id` label), so alerts and charts sum across
  series; a restarted process starts new ones.
- The cost alert sums 24 hours (`86400s`, within the 25-hour limit of
  alerting alignment periods) of an *estimated* cost, not the bill.

## Checks

```bash
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

CI runs these (`.github/workflows/ci-terraform.yml`), and
`backend/tests/test_gcp_deploy_config.py` pins the security decisions above.
No CI job holds Google credentials or runs `plan` or `apply`.

## Caveats

- Custom IAM roles (`automaintainerWorkerRoller`,
  `automaintainerZoneOperationReader`) are soft-deleted on destroy and their
  ids stay reserved for weeks; an apply soon after a destroy in the same
  project fails on them. Undelete them (`gcloud iam roles undelete`) or
  change the `role_id`s.
- The Redis image is pinned by tag and digest; bump both together. The VM
  boots from the `cos-stable` image family, resolved once at creation (the
  boot disk ignores later images, so a new COS release never recreates the
  VM; recreate it deliberately to move to one). The Cloud Run placeholder
  image is unpinned; it runs only until the first deploy and is ignored
  afterwards.
- The worker container uses the host network and can reach the metadata
  server, so code running in it can obtain the worker service account's
  token and read the worker's secrets. That is acceptable only while
  verification of untrusted code is off (`VERIFY_ALLOW_LOCAL` refused).
  Sandboxed verification (#158) must not run on this network.
- `deletion_protection` (default `true`) guards the Cloud Run service and the
  VM; set it to `false` and apply before a deliberate destroy.
- Changing `app_env`, `var.secrets` or the Redis mode updates the VM's
  metadata in place, but the VM reads it only at boot: reset the VM after
  such a change.
