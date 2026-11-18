"""Static checks of the Google Cloud deployment (#527).

infra/terraform holds the Terraform for the GCP project and
.github/workflows/deploy-gcp.yml deploys to it. ``terraform validate`` (run
by ci-terraform.yml) checks that the configuration is well formed; these
tests pin the security and topology decisions that validation cannot see.
They parse text only: no Terraform, no Google credentials, no network.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TF = REPO_ROOT / "infra" / "terraform"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
BACKEND = REPO_ROOT / "backend"

# Every secret the backend reads from its environment (see
# docs/DEPLOYMENT.md, "Environment variables").
BACKEND_SECRETS = {
    "SUPABASE_SERVICE_KEY",
    "GROQ_API_KEY",
    "GITHUB_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_APP_CLIENT_SECRET",
}


def _tf_files():
    return sorted(TF.rglob("*.tf"))


def _all_tf() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _tf_files())


def _strip_comments(hcl: str) -> str:
    return "\n".join(re.sub(r"\s*(#|//).*$", "", line) for line in hcl.splitlines())


def _block(text: str, header: str) -> str:
    """The body of the first block whose header line matches ``header``."""
    m = re.search(header + r"\s*\{", text)
    assert m, header
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(wf: dict) -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    on = wf.get("on", wf.get(True))
    return {k: v for k, v in on.items()} if isinstance(on, dict) else {on: None}


# --- Terraform: layout and state ------------------------------------------


def test_terraform_tree_exists():
    for rel in (
        "versions.tf",
        "variables.tf",
        "outputs.tf",
        "terraform.tfvars.example",
        "README.md",
        ".terraform.lock.hcl",
        "modules/network/main.tf",
        "modules/worker/main.tf",
        "modules/github_wif/main.tf",
    ):
        assert (TF / rel).is_file(), rel


def test_state_is_local_by_default_with_a_partial_gcs_backend_example():
    # No committed backend: `terraform init` keeps state local until the
    # owner opts in to the bucket.
    assert "backend " not in _strip_comments(_all_tf())
    example = _strip_comments((TF / "backend.tf.example").read_text(encoding="utf-8"))
    gcs = _block(example, r'backend\s+"gcs"')
    assert "bucket" not in gcs  # supplied with -backend-config, never committed


def test_no_tfvars_or_state_is_committed():
    ignore = (TF / ".gitignore").read_text(encoding="utf-8").split()
    for pattern in (
        "*.tfvars",
        ".terraform/",
        "*.tfstate",
        "*.tfstate.*",
        "backend.tf",
    ):
        assert pattern in ignore, pattern
    assert not [p for p in TF.rglob("*.tfvars")]
    assert not [p for p in TF.rglob("*.tfstate*")]


# --- Terraform: secrets -----------------------------------------------------


def test_terraform_never_holds_secret_values_or_keys():
    tf = _strip_comments(_all_tf())
    # Containers only: the owner adds versions outside Terraform, so no
    # secret value ever reaches the state file.
    assert "google_secret_manager_secret_version" not in tf
    assert "random_password" not in tf
    # Deploys authenticate through Workload Identity Federation.
    assert "google_service_account_key" not in tf


def test_every_backend_secret_has_a_secret_manager_container():
    variables = (TF / "variables.tf").read_text(encoding="utf-8")
    body = _block(variables, r'variable\s+"secrets"')
    default = _block(body, r"default\s*=")
    names = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*\{", default, re.M))
    assert names == BACKEND_SECRETS


@pytest.mark.parametrize("name", sorted(BACKEND_SECRETS))
def test_listed_secrets_are_read_by_the_backend(name):
    sources = [
        p
        for p in BACKEND.rglob("*.py")
        if ".venv" not in p.parts and "venv" not in p.parts and "test" not in p.name
    ]
    assert any(name in p.read_text(encoding="utf-8") for p in sources), name


def test_example_tfvars_has_no_secret_values():
    example = (TF / "terraform.tfvars.example").read_text(encoding="utf-8")
    for name in BACKEND_SECRETS:
        assert not re.search(rf"^\s*{name}\s*=", example, re.M), name
    assert "BEGIN" not in example and "gsk_" not in example


# --- Terraform: services ------------------------------------------------------


def test_cloud_run_api_keeps_one_warm_instance_and_probes_healthz():
    tf = _strip_comments(_all_tf())
    run = _block(tf, r'resource\s+"google_cloud_run_v2_service"\s+"api"')
    assert re.search(r"min_instance_count\s*=\s*1\b", run)
    assert re.search(r"max_instance_count\s*=\s*var\.api_max_instances", run)
    assert re.search(r"max_instance_request_concurrency\s*=\s*var\.", run)
    assert run.count('path = "/healthz"') >= 2  # startup and liveness probes
    assert re.search(r'\bingress\s*=\s*"INGRESS_TRAFFIC_ALL"', run)
    assert re.search(r'egress\s*=\s*"PRIVATE_RANGES_ONLY"', run)
    invoker = _block(
        tf, r'resource\s+"google_cloud_run_v2_service_iam_member"\s+"public"'
    )
    assert '"allUsers"' in invoker and '"roles/run.invoker"' in invoker


def test_worker_vm_has_no_public_ip_and_egress_goes_through_nat():
    worker = _strip_comments(
        (TF / "modules/worker/main.tf").read_text(encoding="utf-8")
    )
    instance = _block(worker, r'resource\s+"google_compute_instance"\s+"worker"')
    assert "access_config" not in instance
    assert "cos-cloud" in worker
    network = (TF / "modules/network/main.tf").read_text(encoding="utf-8")
    assert 'resource "google_compute_router_nat"' in network


def test_self_hosted_redis_image_is_pinned_by_digest():
    worker = (TF / "modules/worker/main.tf").read_text(encoding="utf-8")
    body = _block(worker, r'variable\s+"redis_image"')
    assert re.search(r'default\s*=\s*"redis:[^"@]+@sha256:[0-9a-f]{64}"', body)


def test_redis_defaults_to_the_cheaper_self_hosted_container():
    variables = (TF / "variables.tf").read_text(encoding="utf-8")
    body = _block(variables, r'variable\s+"redis_mode"')
    assert re.search(r'default\s*=\s*"vm"', body)
    assert "memorystore" in body


def test_dangerous_app_settings_are_rejected():
    variables = (TF / "variables.tf").read_text(encoding="utf-8")
    body = _block(variables, r'variable\s+"app_env"')
    for name in ("AUTH_DEV_BYPASS", "ENABLE_TERMINAL", "VERIFY_ALLOW_LOCAL"):
        assert name in body, name


def test_workload_identity_is_limited_to_this_repository():
    wif = _strip_comments(
        (TF / "modules/github_wif/main.tf").read_text(encoding="utf-8")
    )
    m = re.search(r"attribute_condition\s*=\s*join\((.*?)\)\n", wif, re.S)
    assert m, "attribute_condition must be built with join()"
    condition = m.group(1)
    assert "assertion.repository ==" in condition
    assert "assertion.environment ==" in condition
    # Google checks the ref itself, so the GitHub environment's branch
    # policy (set by hand) is not the only gate: only main and release
    # tags can mint deployer credentials.
    assert 'assertion.ref == \\"refs/heads/main\\"' in condition
    assert 'assertion.ref.startsWith(\\"refs/tags/v\\")' in condition
    # Numeric ids: a renamed or re-created repository cannot take the name.
    assert "assertion.repository_id ==" in condition
    assert "assertion.repository_owner_id ==" in condition
    # Raw text: _strip_comments would cut the principalSet:// URL.
    binding = _block(
        (TF / "modules/github_wif/main.tf").read_text(encoding="utf-8"),
        r'resource\s+"google_service_account_iam_member"\s+"deployer_wif"',
    )
    assert "/attribute.repository_id/${var.github_repository_id}" in binding
    variables = (TF / "variables.tf").read_text(encoding="utf-8")
    assert '"PxA-Labs/AutoMaintainer"' in _block(
        variables, r'variable\s+"github_repository"'
    )
    # gh api repos/PxA-Labs/AutoMaintainer --jq '.id, .owner.id'
    assert '"1254396219"' in _block(variables, r'variable\s+"github_repository_id"')
    assert '"284408399"' in _block(
        variables, r'variable\s+"github_repository_owner_id"'
    )


def test_deploy_checks_every_repository_variable_it_reads():
    text = (WORKFLOWS / "deploy-gcp.yml").read_text(encoding="utf-8")
    loop = re.search(r"for v in ([^;]+);", text)
    assert loop
    checked = {f"GCP_{v}" for v in loop.group(1).split()}
    read = set(re.findall(r"vars\.(GCP_[A-Z_]+)", text))
    read.discard("GCP_WORKLOAD_IDENTITY_PROVIDER")  # the job's `if` needs it
    assert read <= checked, read - checked


def test_a_billing_budget_with_thresholds_exists():
    tf = _strip_comments(_all_tf())
    budget = _block(tf, r'resource\s+"google_billing_budget"\s+"project"')
    assert "var.billing_account_id" in budget
    assert "threshold_rules" in budget


@pytest.mark.parametrize(
    "name", ["api_url", "image_repository", "wif_provider", "deployer_service_account"]
)
def test_required_outputs(name):
    outputs = (TF / "outputs.tf").read_text(encoding="utf-8")
    assert re.search(rf'output\s+"{name}"\s*\{{', outputs), name


# --- Workflows ----------------------------------------------------------------


def test_deploy_runs_only_on_dispatch_and_tags():
    wf = _load_workflow("deploy-gcp.yml")
    triggers = _triggers(wf)
    assert set(triggers) == {"workflow_dispatch", "push"}
    assert set(triggers["push"]) == {"tags"}


def test_deploy_needs_a_protected_environment_and_uses_wif():
    wf = _load_workflow("deploy-gcp.yml")
    assert wf["permissions"] == {"contents": "read"}
    assert len(wf["concurrency"]["group"]) > 0
    assert wf["concurrency"]["cancel-in-progress"] is False
    for name, job in wf["jobs"].items():
        assert job.get("environment"), name
        # Inert until the owner sets the repository variables.
        assert "vars.GCP_WORKLOAD_IDENTITY_PROVIDER != ''" in job["if"], name
        assert job["permissions"] == {"contents": "read", "id-token": "write"}
        auth = [
            s
            for s in job["steps"]
            if s.get("uses", "").startswith("google-github-actions/auth@")
        ]
        assert len(auth) == 1, name
        assert auth[0]["with"]["workload_identity_provider"] == (
            "${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}"
        )
        assert "credentials_json" not in auth[0]["with"]
    text = (WORKFLOWS / "deploy-gcp.yml").read_text(encoding="utf-8")
    assert "secrets." not in text  # nothing but variables and the OIDC token
    assert "gcloud run deploy" in text or "gcloud run services update" in text
    assert "automaintainer-image=" in text  # rolls the worker VM


def test_terraform_ci_checks_format_and_validates_read_only():
    wf = _load_workflow("ci-terraform.yml")
    assert wf["permissions"] == {"contents": "read"}
    runs = [s.get("run", "") for job in wf["jobs"].values() for s in job["steps"]]
    joined = "\n".join(runs)
    assert "terraform fmt -check -recursive" in joined
    assert "terraform init -backend=false" in joined
    assert "terraform validate" in joined
    text = (WORKFLOWS / "ci-terraform.yml").read_text(encoding="utf-8")
    assert "id-token" not in text and "apply" not in text
