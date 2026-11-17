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
one VM through a custom role, plus read zone operations. The WIF provider
accepts only tokens whose `repository` is `PxA-Labs/AutoMaintainer` and
whose `environment` is `production`.

**Dangerous settings are refused.** `app_env` may not set
`AUTH_DEV_BYPASS`, `ENABLE_TERMINAL` or `VERIFY_ALLOW_LOCAL`, nor the values
Terraform owns (`ENVIRONMENT=production`, `REDIS_URL`, `PORT`).

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

- Custom IAM roles are soft-deleted on destroy and their ids stay reserved
  for weeks; re-creating the configuration in the same project soon after a
  destroy needs new `role_id`s.
- `deletion_protection` (default `true`) guards the Cloud Run service and the
  VM; set it to `false` and apply before a deliberate destroy.
- Changing `app_env`, `var.secrets` or the Redis mode updates the VM's
  metadata in place, but the VM reads it only at boot: reset the VM after
  such a change.
