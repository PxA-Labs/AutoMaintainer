"""Static checks of the sandboxed verification infrastructure (#158, #527).

infra/terraform/sandbox.tf runs each verification command as one Cloud Run
job execution (automaintainer/runners/cloud_run_job_sandbox.py). ``terraform
validate`` (ci-terraform.yml) checks the syntax; these tests pin the
isolation decisions it cannot see: the job's service account holds no role,
its network has no route or NAT to the internet and denies egress, the bucket
is private and self-cleaning, and the worker gets exactly what it needs to
run the job. Text only: no Terraform, no credentials, no network.
"""

import re
from pathlib import Path

import pytest

from automaintainer.runners import cloud_run_job_sandbox as crj
from automaintainer.runners.sandbox import SANDBOX_BACKEND_ALIASES

REPO_ROOT = Path(__file__).resolve().parents[2]
TF = REPO_ROOT / "infra" / "terraform"
SANDBOX_TF = TF / "sandbox.tf"
WORKER_TF = TF / "modules" / "worker" / "main.tf"

# private.googleapis.com: Private Google Access for the default API domains.
PRIVATE_GOOGLE_ACCESS = "199.36.153.8/30"


def _strip_comments(hcl: str) -> str:
    return "\n".join(re.sub(r"\s*(#|//).*$", "", line) for line in hcl.splitlines())


def _read(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _all_tf() -> str:
    return "\n".join(_read(p) for p in sorted(TF.rglob("*.tf")))


def _block(text: str, header: str) -> str:
    """The body of the first block whose header matches ``header``."""
    m = re.search(header + r"\s*\{", text)
    assert m, header
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _resources(text: str, kind: str) -> dict:
    """ "<type>.<name>" -> body of every resource whose type matches ``kind``
    (a regular expression)."""
    found = re.findall(rf'resource\s+"({kind})"\s+"([\w-]+)"', text)
    return {f"{t}.{n}": _block(text, rf'resource\s+"{t}"\s+"{n}"') for t, n in found}


@pytest.fixture(scope="module")
def sandbox_tf():
    assert SANDBOX_TF.is_file()
    return _read(SANDBOX_TF)


# --- the job ------------------------------------------------------------------


def test_job_runs_the_entrypoint_once_as_the_sandbox_account(sandbox_tf):
    job = _block(sandbox_tf, r'resource\s+"google_cloud_run_v2_job"\s+"sandbox"')
    assert re.search(r"name\s*=\s*module\.worker\.sandbox_job_name", job)
    assert re.search(
        r"service_account\s*=\s*google_service_account\.sandbox\.email", job
    )
    # A failing test must not be retried: one execution, one task, one try.
    assert re.search(r"max_retries\s*=\s*0\b", job)
    assert re.search(r"task_count\s*=\s*1\b", job)
    # The worker sends "-I -c <entrypoint> <command>" as the args.
    assert re.search(r'command\s*=\s*\["python3"\]', job)
    assert crj.ENTRYPOINT_ARGS[0] == "-I"
    assert re.search(r"image\s*=\s*var\.sandbox_image", job)
    # Every packet leaves through the sandbox network, which has no way out.
    vpc = _block(job, r"vpc_access")
    assert re.search(r'egress\s*=\s*"ALL_TRAFFIC"', vpc)
    assert re.search(r"network\s*=\s*google_compute_network\.sandbox\.id", vpc)
    assert re.search(r"subnetwork\s*=\s*google_compute_subnetwork\.sandbox\.id", vpc)


def test_job_service_account_has_no_role_anywhere():
    tf = _all_tf()
    # The account is used once, as the job's identity, and is never a member
    # of any IAM binding, project-level or not.
    assert 'resource "google_service_account" "sandbox"' in tf
    uses = re.findall(r"google_service_account\.sandbox\.(\w+)", tf)
    assert set(uses) == {"email"}
    bindings = _resources(tf, r"google_\w*iam\w*")
    assert bindings
    for name, body in bindings.items():
        assert "google_service_account.sandbox" not in body, name
    project_bindings = r"google_project_iam_(?:member|binding|policy)"
    for name, body in _resources(tf, project_bindings).items():
        assert "sandbox" not in body, name


# --- network ------------------------------------------------------------------


def test_sandbox_network_is_dedicated_without_default_routes_or_nat(sandbox_tf):
    network = _block(sandbox_tf, r'resource\s+"google_compute_network"\s+"sandbox"')
    assert re.search(r"auto_create_subnetworks\s*=\s*false", network)
    # No 0.0.0.0/0 route at all: nothing reaches the internet even if a
    # firewall rule were wrong.
    assert re.search(r"delete_default_routes_on_create\s*=\s*true", network)
    subnet = _block(sandbox_tf, r'resource\s+"google_compute_subnetwork"\s+"sandbox"')
    assert re.search(r"network\s*=\s*google_compute_network\.sandbox\.id", subnet)
    assert re.search(r"private_ip_google_access\s*=\s*true", subnet)
    assert "google_compute_router" not in sandbox_tf
    assert "google_compute_router_nat" not in sandbox_tf
    # Not peered with, and not a subnet of, the worker's network.
    assert "module.network" not in sandbox_tf
    assert "google_compute_network_peering" not in _all_tf()
    routes = _resources(sandbox_tf, "google_compute_route")
    assert len(routes) == 1
    [route] = routes.values()
    assert f'dest_range       = "{PRIVATE_GOOGLE_ACCESS}"' in route or re.search(
        rf'dest_range\s*=\s*"{re.escape(PRIVATE_GOOGLE_ACCESS)}"', route
    )


def test_firewall_denies_all_egress_but_google_apis(sandbox_tf):
    rules = _resources(sandbox_tf, "google_compute_firewall")
    assert all(
        re.search(r"network\s*=\s*google_compute_network\.sandbox\.id", b)
        for b in rules.values()
    )
    egress = {
        n: b for n, b in rules.items() if re.search(r'direction\s*=\s*"EGRESS"', b)
    }
    deny = [b for b in egress.values() if re.search(r"\bdeny\s*\{", b)]
    allow = [b for b in egress.values() if re.search(r"\ballow\s*\{", b)]
    assert len(deny) == 1 and len(allow) == 1
    [deny], [allow] = deny, allow
    assert re.search(r'destination_ranges\s*=\s*\["0\.0\.0\.0/0"\]', deny)
    assert re.search(r'protocol\s*=\s*"all"', deny)
    assert not re.search(r"target_tags|target_service_accounts", deny)
    assert re.search(
        rf'destination_ranges\s*=\s*\["{re.escape(PRIVATE_GOOGLE_ACCESS)}"\]', allow
    )
    assert re.search(r'ports\s*=\s*\["443"\]', allow)
    # Lower number wins: the narrow allow is evaluated before the deny, and
    # the deny before the implied allow-all egress rule (65535).
    deny_priority = int(re.search(r"priority\s*=\s*(\d+)", deny).group(1))
    allow_priority = int(re.search(r"priority\s*=\s*(\d+)", allow).group(1))
    assert allow_priority < deny_priority < 65535
    # Nothing may connect in.
    assert not [b for b in rules.values() if re.search(r'"INGRESS"', b)]


def test_storage_resolves_to_private_google_access_only(sandbox_tf):
    zone = _block(
        sandbox_tf, r'resource\s+"google_dns_managed_zone"\s+"sandbox_storage"'
    )
    assert re.search(r'dns_name\s*=\s*"storage\.googleapis\.com\."', zone)
    assert re.search(r'visibility\s*=\s*"private"', zone)
    assert "google_compute_network.sandbox.id" in zone
    record = _block(
        sandbox_tf, r'resource\s+"google_dns_record_set"\s+"sandbox_storage"'
    )
    for ip in ("199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"):
        assert f'"{ip}"' in record


# --- bucket ---------------------------------------------------------------------


def test_bucket_is_private_and_deletes_objects_after_a_day(sandbox_tf):
    bucket = _block(sandbox_tf, r'resource\s+"google_storage_bucket"\s+"sandbox"')
    assert re.search(r"name\s*=\s*module\.worker\.sandbox_bucket_name", bucket)
    assert re.search(r"uniform_bucket_level_access\s*=\s*true", bucket)
    assert re.search(r'public_access_prevention\s*=\s*"enforced"', bucket)
    rule = _block(bucket, r"lifecycle_rule")
    assert re.search(r"age\s*=\s*1\b", rule)
    assert re.search(r'type\s*=\s*"Delete"', rule)
    bindings = _resources(sandbox_tf, r"google_storage_bucket_iam_\w+")
    assert bindings, "the worker needs object access"
    for body in bindings.values():
        assert "allUsers" not in body and "allAuthenticatedUsers" not in body
        assert re.search(r"member\s*=\s*google_service_account\.worker\.member", body)
        assert re.search(r'role\s*=\s*"roles/storage\.objectUser"', body)


# --- the worker's permissions ----------------------------------------------------


def test_worker_can_run_and_cancel_the_job_and_nothing_more(sandbox_tf):
    role = _block(
        sandbox_tf, r'resource\s+"google_project_iam_custom_role"\s+"sandbox_runner"'
    )
    permissions = set(re.findall(r'"(run\.[\w.]+)"', role))
    assert permissions == {
        "run.jobs.get",
        "run.jobs.run",
        "run.jobs.runWithOverrides",
        "run.executions.get",
        "run.executions.cancel",
        "run.operations.get",
    }
    [binding] = _resources(sandbox_tf, "google_cloud_run_v2_job_iam_member").values()
    assert "google_cloud_run_v2_job.sandbox.name" in binding
    assert "google_project_iam_custom_role.sandbox_runner.id" in binding
    assert re.search(r"member\s*=\s*google_service_account\.worker\.member", binding)
    # The role is granted on the job only, never on the project.
    for body in _resources(_all_tf(), "google_project_iam_member").values():
        assert "sandbox_runner" not in body


def test_worker_signs_urls_as_itself_only(sandbox_tf):
    bindings = _resources(sandbox_tf, "google_service_account_iam_member")
    [signer] = bindings.values()
    assert re.search(
        r"service_account_id\s*=\s*google_service_account\.worker\.name", signer
    )
    assert re.search(r"member\s*=\s*google_service_account\.worker\.member", signer)
    assert re.search(r'role\s*=\s*"roles/iam\.serviceAccountTokenCreator"', signer)


# --- the worker container ----------------------------------------------------------


def _worker_container_env() -> dict:
    worker = _read(WORKER_TF)
    body = _block(worker, r"container_env\s*=\s*merge\(var\.app_env,")
    return dict(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.+?)\s*$", body, re.M))


def test_worker_container_selects_the_cloud_run_backend():
    env = _worker_container_env()
    assert env["SANDBOX_BACKEND"] == '"cloud_run_job"'
    assert "cloud_run_job" in SANDBOX_BACKEND_ALIASES
    assert env["CLOUD_RUN_SANDBOX_JOB"] == "local.sandbox_job_name"
    assert env["CLOUD_RUN_SANDBOX_BUCKET"] == "local.sandbox_bucket_name"
    assert env["CLOUD_RUN_SANDBOX_REGION"] == "var.region"
    assert env["CLOUD_RUN_SANDBOX_SIGNER"] == "var.service_account_email"
    assert "CLOUD_RUN_SANDBOX_PROJECT" in env
    # Local verification stays refused: the worker never opts in.
    assert "VERIFY_ALLOW_LOCAL" not in env
    assert "VERIFY_ALLOW_LOCAL" not in _read(WORKER_TF)


def test_worker_settings_are_what_the_backend_reads():
    names = [n for n in _worker_container_env() if n.startswith("CLOUD_RUN_SANDBOX_")]
    sample = {
        "CLOUD_RUN_SANDBOX_PROJECT": "automaintainer-prod",
        "CLOUD_RUN_SANDBOX_JOB": "automaintainer-sandbox",
        "CLOUD_RUN_SANDBOX_BUCKET": "automaintainer-prod-automaintainer-sandbox",
        "CLOUD_RUN_SANDBOX_REGION": "us-central1",
        "CLOUD_RUN_SANDBOX_SIGNER": "automaintainer-worker@x.iam.gserviceaccount.com",
    }
    assert set(names) == set(sample)
    config = crj.CloudRunJobSandboxConfig.from_env(sample)
    assert config.job_name == (
        "projects/automaintainer-prod/locations/us-central1/jobs/automaintainer-sandbox"
    )


def test_job_and_bucket_names_come_from_the_worker_module():
    worker = _read(WORKER_TF)
    assert re.search(r'sandbox_job_name\s*=\s*"\$\{var\.name\}-sandbox"', worker)
    assert re.search(
        r'sandbox_bucket_name\s*=\s*"\$\{data\.google_client_config\.current\.project\}-\$\{var\.name\}-sandbox"',
        worker,
    )
    for name in ("sandbox_job_name", "sandbox_bucket_name"):
        assert re.search(rf'output\s+"{name}"', worker), name


def test_sandbox_outputs_and_variables_live_in_sandbox_tf(sandbox_tf):
    for name in ("sandbox_job", "sandbox_bucket", "sandbox_service_account"):
        assert re.search(rf'output\s+"{name}"', sandbox_tf), name
    for name in (
        "sandbox_image",
        "sandbox_subnet_cidr",
        "sandbox_cpu",
        "sandbox_memory",
    ):
        assert re.search(rf'variable\s+"{name}"', sandbox_tf), name
