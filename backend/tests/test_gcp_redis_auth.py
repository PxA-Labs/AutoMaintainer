"""Redis AUTH on Google Cloud (#532).

Cloud Run reaches Redis over the VPC, so a server-side request forgery in
the API could reach it. Redis therefore requires a password, which lives
only in Secret Manager: the owner adds the full broker URL,
``redis://:<password>@<host>:<port>/0``, as a version of the secret
``redis-url`` (Terraform creates the container only, like every other
secret, so the password is never in the Terraform state). From there:

- the API gets REDIS_URL from the secret (Cloud Run secret env var);
- the worker gets it through secrets_exec.py at container start;
- the self-hosted Redis container (redis_mode "vm") reads the password from
  the same secret and starts with ``requirepass``, passed on stdin, never on
  a command line;
- Memorystore (redis_mode "memorystore") has AUTH enabled; Google generates
  its AUTH string, which the owner puts into the same URL.

These tests parse text, plus one that runs run-redis.sh under bash with
fake docker, curl and iptables. No Terraform, Google or Redis is reached.
"""

import base64
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TF = REPO_ROOT / "infra" / "terraform"
WORKER = TF / "modules" / "worker"


def _strip_comments(hcl: str) -> str:
    return "\n".join(re.sub(r"\s*(#|//).*$", "", line) for line in hcl.splitlines())


def _tf(rel: str) -> str:
    return _strip_comments((TF / rel).read_text(encoding="utf-8"))


def _block(text: str, header: str) -> str:
    m = re.search(header + r"\s*\{", text)
    assert m, header
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _all_tf() -> str:
    return "\n".join(
        _strip_comments(p.read_text(encoding="utf-8")) for p in sorted(TF.rglob("*.tf"))
    )


# --- Terraform ------------------------------------------------------------------


def test_redis_auth_is_on_by_default():
    var = _block(_tf("variables.tf"), r'variable\s+"redis_auth_enabled"')
    assert re.search(r"type\s*=\s*bool", var)
    assert re.search(r"default\s*=\s*true", var)


def test_the_redis_url_secret_is_a_container_only():
    secrets = _tf("secrets.tf")
    secret = _block(secrets, r'resource\s+"google_secret_manager_secret"\s+"redis_url"')
    assert re.search(r"count\s*=\s*var\.redis_auth_enabled\s*\?\s*1\s*:\s*0", secret)
    assert re.search(r'secret_id\s*=\s*"redis-url"', secret)
    tf = _all_tf()
    # No generated password and no version: nothing secret in the state.
    assert "google_secret_manager_secret_version" not in tf
    assert "random_password" not in tf
    assert "random_string" not in tf
    for who in ("api", "worker"):
        grant = _block(
            secrets,
            rf'resource\s+"google_secret_manager_secret_iam_member"\s+"redis_url_{who}"',
        )
        assert "google_secret_manager_secret.redis_url[0].id" in grant
        assert '"roles/secretmanager.secretAccessor"' in grant
        assert f"google_service_account.{who}.member" in grant


def test_the_api_reads_redis_url_from_the_secret():
    run = _tf("run.tf")
    # The plain URL (no password) is set only when the secret is not used.
    assert re.search(
        r"api_redis_url_from_secret\s*=\s*var\.redis_auth_enabled\s*&&\s*var\.mount_secrets",
        run,
    )
    assert re.search(
        r"local\.api_redis_url_from_secret\s*\?\s*\{\}\s*:\s*\{\s*REDIS_URL\s*=\s*local\.redis_url\s*\}",
        run,
    )
    assert re.search(
        r"api_secret_refs\s*=\s*merge\([^)]*local\.api_redis_url_from_secret\s*\?\s*"
        r"\{\s*REDIS_URL\s*=\s*google_secret_manager_secret\.redis_url\[0\]\.secret_id\s*\}",
        run,
    )
    service = _block(run, r'resource\s+"google_cloud_run_v2_service"\s+"api"')
    assert "for_each = local.api_secret_refs" in service
    assert "google_secret_manager_secret_iam_member.redis_url_api" in service


def test_the_worker_reads_redis_url_from_the_secret():
    main = _tf("main.tf")
    worker = _block(main, r'module\s+"worker"')
    assert re.search(
        r"var\.redis_auth_enabled\s*\?\s*\{\s*REDIS_URL\s*=\s*google_secret_manager_secret\.redis_url\[0\]\.id\s*\}\s*:\s*\{\}",
        worker,
    )
    assert "google_secret_manager_secret_iam_member.redis_url_worker" in worker
    module = _tf("modules/worker/main.tf")
    # No plain REDIS_URL in the env file when the secret provides it, and the
    # secret's name goes to run-redis.sh through worker.env.
    assert re.search(
        r'redis_url_secret\s*=\s*lookup\(var\.secrets,\s*"REDIS_URL",\s*""\)', module
    )
    assert '"REDIS_URL_SECRET=${local.redis_url_secret}"' in module
    container_env = module.split("container_env = merge(", 1)[1].split("user_data", 1)[
        0
    ]
    assert re.search(
        r"local\.redis_url_secret\s*==\s*\"\"\s*\?\s*\{\s*REDIS_URL\s*=", container_env
    )
    assert container_env.count("REDIS_URL") == 1


def test_memorystore_has_auth_enabled():
    broker = _block(_tf("main.tf"), r'resource\s+"google_redis_instance"\s+"broker"')
    assert re.search(r"auth_enabled\s*=\s*var\.redis_auth_enabled", broker)


def test_outputs_give_the_owner_what_the_url_needs():
    outputs = _tf("outputs.tf")
    address = _block(outputs, r'output\s+"redis_address"')
    assert "local.redis_host_port" in address
    secret = _block(outputs, r'output\s+"redis_url_secret_id"')
    assert "google_secret_manager_secret.redis_url" in secret


def test_the_owner_step_is_documented():
    deploy = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    option_d = deploy.split("## Option D", 1)[1].split("\n## ", 1)[0]
    for needle in (
        "redis_auth_enabled",
        "redis-url",
        "openssl rand -hex 32",
        "terraform output -raw redis_address",
        "get-auth-string",
        "requirepass",
    ):
        assert needle in option_d, needle
    assert "is not set up yet" not in option_d
    readme = (TF / "README.md").read_text(encoding="utf-8")
    assert "redis-url" in readme and "requirepass" in readme


# --- run-redis.sh ---------------------------------------------------------------


def test_run_redis_passes_the_password_on_stdin_only():
    script = (WORKER / "run-redis.sh").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "set -x" not in code
    assert "--requirepass" not in code
    assert "redis-server -" in code
    assert "requirepass" in code


BASH = shutil.which("bash")


def _fake_bin(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def sandbox(tmp_path):
    if BASH is None:
        pytest.skip("bash is not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    log.mkdir()
    # docker: record the arguments and stdin of `docker run`.
    _fake_bin(
        bin_dir,
        "docker",
        'if [ "$1" = run ]; then\n'
        f'  printf "%s\\n" "$@" > "{log.as_posix()}/docker-args"\n'
        f'  cat > "{log.as_posix()}/docker-stdin"\n'
        "fi\n",
    )
    _fake_bin(bin_dir, "iptables", "exit 0\n")
    # curl: the metadata token, then the secret; record every argument.
    _fake_bin(
        bin_dir,
        "curl",
        f'printf "%s\\n" "$@" >> "{log.as_posix()}/curl-args"\n'
        f'cat >> "{log.as_posix()}/curl-stdin"\n'
        'case "$*" in\n'
        '  *service-accounts/default/token*) cat "$FAKE_TOKEN" ;;\n'
        '  *secretmanager.googleapis.com*) cat "$FAKE_SECRET" ;;\n'
        "  *) exit 22 ;;\n"
        "esac\n",
    )
    return tmp_path, bin_dir, log


def _run_redis(sandbox, secret_value=None, secret_name="projects/p/secrets/redis-url"):
    tmp, bin_dir, log = sandbox
    env_file = tmp / "worker.env"
    env_file.write_text(
        "REDIS_IMAGE=redis:test\nREDIS_MAXMEMORY=64mb\n"
        "REDIS_ALLOW_CIDR=10.10.0.0/24\n"
        f"REDIS_URL_SECRET={secret_name if secret_value is not None else ''}\n",
        encoding="utf-8",
        newline="\n",
    )
    token = tmp / "token.json"
    token.write_text(json.dumps({"access_token": "tok-123", "expires_in": 3599}))
    secret = tmp / "secret.json"
    data = base64.b64encode((secret_value or "").encode()).decode()
    secret.write_text(
        json.dumps({"name": secret_name, "payload": {"data": data, "dataCrc32c": "1"}}),
        encoding="utf-8",
    )
    env = {
        "PATH": f"{bin_dir.as_posix()}:/usr/bin:/bin",
        "AUTOMAINTAINER_WORKER_ENV": env_file.as_posix(),
        "FAKE_TOKEN": token.as_posix(),
        "FAKE_SECRET": secret.as_posix(),
    }
    if os.name == "nt":  # Git Bash needs its own PATH entries and SYSTEMROOT
        env["PATH"] = f"{bin_dir.as_posix()}:" + os.environ.get("PATH", "")
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    proc = subprocess.run(
        [BASH, (WORKER / "run-redis.sh").as_posix()],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    read = lambda name: (  # noqa: E731
        (log / name).read_text(encoding="utf-8") if (log / name).exists() else ""
    )
    return proc, read("docker-args"), read("docker-stdin"), read("curl-args")


PASSWORD = "0123456789abcdef0123456789abcdef"  # gitleaks:allow (test value)


def test_run_redis_starts_redis_with_requirepass_from_the_secret(sandbox):
    proc, args, stdin, curl_args = _run_redis(
        sandbox, f"redis://:{PASSWORD}@10.10.0.2:6379/0"
    )
    assert proc.returncode == 0, proc.stderr
    assert stdin == f"requirepass {PASSWORD}\n"
    assert "redis-server\n-\n" in args  # config from stdin
    assert "-i\n" in args
    # Neither the password nor the token is on any command line or in the logs.
    for text in (args, curl_args, proc.stdout, proc.stderr):
        assert PASSWORD not in text
        assert "tok-123" not in text
    assert "projects/p/secrets/redis-url/versions/latest:access" in curl_args


def test_run_redis_without_auth_has_no_password(sandbox):
    proc, args, stdin, curl_args = _run_redis(sandbox, None)
    assert proc.returncode == 0, proc.stderr
    assert stdin == ""
    assert "requirepass" not in args
    assert curl_args == ""


@pytest.mark.parametrize(
    "value",
    [
        "redis://10.10.0.2:6379/0",  # no password
        "redis://:short@10.10.0.2:6379/0",  # too short
        "redis://:p%40ss-word-with-escapes-0000@10.10.0.2:6379/0",  # not URL-safe
    ],
)
def test_run_redis_refuses_an_unusable_password_without_printing_it(sandbox, value):
    proc, args, stdin, _ = _run_redis(sandbox, value)
    assert proc.returncode != 0
    assert args == "" and stdin == ""
    assert "REDIS_URL_SECRET" in proc.stderr
    assert value not in proc.stderr and value not in proc.stdout
