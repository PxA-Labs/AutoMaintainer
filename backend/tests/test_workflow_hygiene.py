"""Static checks on the GitHub Actions workflows (#406, #410).

These guard CI hygiene that used to regress silently:

- every third-party action is pinned to a full 40-character commit SHA with a
  ``# vX.Y.Z`` comment, so Dependabot can bump it and a reviewer can read it;
- every ``github/codeql-action`` step runs the same SHA (mismatched init and
  analyze versions made CodeQL fail on main);
- the top-level token permissions are read-only, and write scopes are
  granted only to the jobs that need them;
- the dashboard CI runs its unit tests (``npm test``), not only lint and build;
- the Supabase keep-alive step fails loudly, naming the missing secret or the
  HTTP status and its likely cause. Its script is run under bash against a
  stub ``curl``, so no network or real project is involved.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
# Disabled workflows (``*.yml.disabled``) are checked too: re-enabling one
# should not bring back an unpinned action.
WORKFLOWS = sorted(
    p
    for p in WORKFLOWS_DIR.iterdir()
    if p.name.endswith((".yml", ".yaml", ".yml.disabled"))
)

USES_RE = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED_RE = re.compile(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}")
VERSION_COMMENT_RE = re.compile(r"\s+# v\d+\.\d+\.\d+\s*$")


def _uses_lines():
    for wf in WORKFLOWS:
        for lineno, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = USES_RE.match(line)
            if m:
                yield wf.name, lineno, m.group("ref"), m.group("rest")


def test_workflows_found():
    assert len(WORKFLOWS) >= 10


@pytest.mark.parametrize(
    "ref, ok",
    [
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", True),
        ("github/codeql-action/init@33d7b85da1a04171e211befe2c739303b9a4c066", True),
        ("actions/checkout@v4", False),
        # One character too long: GitHub cannot resolve it as a commit SHA.
        ("actions/first-interaction@753c925c8d1ac6fede23781875376600628d9b5d3", False),
        ("actions/checkout@3d3c42e5", False),
    ],
)
def test_pin_pattern(ref, ok):
    assert bool(PINNED_RE.fullmatch(ref)) is ok


def test_every_action_is_pinned_to_a_full_sha_with_a_version_comment():
    bad = []
    for name, lineno, ref, rest in _uses_lines():
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        if not PINNED_RE.fullmatch(ref) or not VERSION_COMMENT_RE.fullmatch(rest):
            bad.append(f"{name}:{lineno}: {ref}{rest}")
    assert not bad, "unpinned or uncommented actions:\n" + "\n".join(bad)


def test_same_action_sha_always_has_the_same_version_comment():
    seen = {}
    for name, lineno, ref, rest in _uses_lines():
        if "@" not in ref:
            continue
        sha = ref.split("@", 1)[1]
        seen.setdefault(sha, set()).add(rest.strip())
    conflicting = {sha: c for sha, c in seen.items() if len(c) > 1}
    assert not conflicting


def test_all_codeql_action_steps_use_one_sha():
    shas = {
        ref.split("@", 1)[1]
        for _, _, ref, _ in _uses_lines()
        if ref.startswith("github/codeql-action/")
    }
    # init, autobuild and analyze in codeql-analysis.yml, upload-sarif in
    # scorecard.yml.
    assert len(shas) == 1, shas


def _load(wf):
    return yaml.safe_load(wf.read_text(encoding="utf-8"))


def test_workflows_are_valid_yaml():
    for wf in WORKFLOWS:
        data = _load(wf)
        assert isinstance(data, dict) and "jobs" in data, wf.name


def test_top_level_permissions_are_read_only():
    # The workflow-wide default is contents: read (or no scopes at all); any
    # write scope is granted per job, so a new job never inherits write access.
    for wf in WORKFLOWS:
        perms = _load(wf).get("permissions")
        assert perms in ({"contents": "read"}, {}), f"{wf.name}: {perms!r}"


def _run_commands(job):
    return [step.get("run", "").strip() for step in job["steps"]]


def test_frontend_ci_runs_lint_unit_tests_build_and_e2e():
    wf = _load(WORKFLOWS_DIR / "ci-frontend.yml")
    commands = [c for job in wf["jobs"].values() for c in _run_commands(job)]
    for required in ("npm run lint", "npm test", "npm run build", "npm run test:e2e"):
        assert required in commands, required
    # The dashboard's "test" script is what the step above runs.
    pkg = json.loads((REPO_ROOT / "dashboard" / "package.json").read_text("utf-8"))
    assert pkg["scripts"]["test"].startswith("vitest run")


def test_no_job_uses_blanket_permissions():
    for wf in WORKFLOWS:
        for job_name, job in _load(wf)["jobs"].items():
            perms = job.get("permissions", {})
            assert isinstance(perms, dict), f"{wf.name}:{job_name}: {perms!r}"


# --- Supabase keep-alive -------------------------------------------------

KEEPALIVE = WORKFLOWS_DIR / "supabase-keepalive.yml"

# Stands in for curl: records its arguments, writes FAKE_BODY to the -o file
# and prints FAKE_STATUS the way `curl -w '%{http_code}'` does.
FAKE_CURL = """printf '%s\\n' "$@" > "$CURL_ARGS_FILE"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
if [ -n "$out" ]; then printf '%s' "${FAKE_BODY:-}" > "$out"; fi
if [ -n "${FAKE_STDERR:-}" ]; then echo "$FAKE_STDERR" >&2; fi
printf '%s' "$FAKE_STATUS"
exit "${FAKE_EXIT:-0}"
"""


def _bash():
    """A POSIX bash; on Windows, Git Bash rather than the WSL launcher."""
    candidates = [shutil.which("bash")]
    vcs = shutil.which("git")
    if vcs:
        # Git for Windows: <root>/cmd/git.exe next to <root>/bin/bash.exe.
        candidates.append(str(Path(vcs).resolve().parents[1] / "bin" / "bash.exe"))
    for c in candidates:
        if not c or not Path(c).exists():
            continue
        if os.name == "nt" and ("system32" in c.lower() or "windowsapps" in c.lower()):
            continue
        return c
    return None


BASH = _bash()


def _keepalive_step():
    (step,) = _load(KEEPALIVE)["jobs"]["ping-supabase"]["steps"]
    return step


def test_keepalive_is_read_only_bounded_and_reads_secrets_only_via_env():
    wf = _load(KEEPALIVE)
    job = wf["jobs"]["ping-supabase"]
    assert wf["permissions"] == {} and job["permissions"] == {}
    assert isinstance(job["timeout-minutes"], int) and job["timeout-minutes"] <= 10
    step = _keepalive_step()
    assert step["shell"] == "bash"
    assert "${{" not in step["run"]  # no expression interpolation in the script
    assert set(step["env"]) == {"SUPABASE_URL", "SUPABASE_SERVICE_KEY"}


@pytest.fixture
def run_keepalive(tmp_path):
    if BASH is None:
        pytest.skip("bash is not available")
    script = tmp_path / "keepalive.sh"
    script.write_bytes(_keepalive_step()["run"].encode("utf-8"))
    fake_curl = tmp_path / "fake-curl.sh"
    fake_curl.write_bytes(FAKE_CURL.encode("utf-8"))
    # A shell function shadows any curl binary, whatever PATH order the bash
    # launcher sets up (Git Bash prepends its own /usr/bin), so the real
    # curl, and the network, are never reached.
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_bytes(
        b'curl() { "$BASH" "$FAKE_CURL_SCRIPT" "$@"; }\n. "$KEEPALIVE_SCRIPT"\n'
    )
    args_file = tmp_path / "curl-args"
    summary = tmp_path / "summary.md"

    def run(status="200", *, url="https://proj.supabase.co/", key="svc", **extra):
        env = {
            **os.environ,
            "FAKE_CURL_SCRIPT": fake_curl.as_posix(),
            "KEEPALIVE_SCRIPT": script.as_posix(),
            "CURL_ARGS_FILE": args_file.as_posix(),
            "GITHUB_STEP_SUMMARY": summary.as_posix(),
            "FAKE_STATUS": status,
            "SUPABASE_URL": url,
            "SUPABASE_SERVICE_KEY": key,
            **extra,
        }
        # The flags GitHub Actions uses for `shell: bash`.
        proc = subprocess.run(
            [BASH, "--noprofile", "--norc", "-eo", "pipefail", wrapper.as_posix()],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return SimpleNamespace(
            code=proc.returncode,
            out=proc.stdout + proc.stderr,
            curl_args=(
                args_file.read_text().splitlines() if args_file.exists() else None
            ),
            summary=summary.read_text() if summary.exists() else "",
        )

    return run


def test_keepalive_succeeds_on_2xx_and_strips_trailing_slash(run_keepalive):
    r = run_keepalive("200")
    assert r.code == 0, r.out
    assert "succeeded (HTTP 200)" in r.out
    assert "https://proj.supabase.co/rest/v1/runs?select=id&limit=1" in r.curl_args
    assert r.summary == ""


@pytest.mark.parametrize(
    "url, key, named",
    [
        ("", "", "SUPABASE_URL SUPABASE_SERVICE_KEY."),
        ("https://proj.supabase.co", "", "SUPABASE_SERVICE_KEY."),
        ("", "svc", "SUPABASE_URL."),
    ],
)
def test_keepalive_names_each_missing_secret(run_keepalive, url, key, named):
    r = run_keepalive(url=url, key=key)
    assert r.code == 1
    assert "::error title=Supabase keep-alive failed::" in r.out
    assert f"Repository secret(s) not set: {named}" in r.out
    assert r.curl_args is None  # never pings with a missing secret
    assert "Owner action" in r.summary and "DEPLOYMENT.md" in r.summary


@pytest.mark.parametrize(
    "status, hint",
    [
        ("401", "SUPABASE_SERVICE_KEY is wrong or was rotated"),
        ("403", "SUPABASE_SERVICE_KEY is wrong or was rotated"),
        ("404", "runs table was not found"),
        ("503", "most likely paused"),
        ("540", "most likely paused"),
        # A redirect never reaches the database, so it is not a successful
        # keep-alive (the previous script accepted any 3xx).
        ("301", "unexpected response"),
    ],
)
def test_keepalive_fails_with_the_http_status_and_a_cause(run_keepalive, status, hint):
    r = run_keepalive(status, FAKE_BODY='{"message":"nope"}')
    assert r.code == 1
    assert (
        "::error title=Supabase keep-alive failed::"
        f"Supabase ping returned HTTP {status}: "
    ) in r.out
    assert hint in r.out
    assert '{"message":"nope"}' in r.out  # response body shown for diagnosis
    assert f"HTTP {status}" in r.summary


def test_keepalive_reports_no_response_when_curl_cannot_connect(run_keepalive):
    r = run_keepalive(
        "000", FAKE_EXIT="6", FAKE_STDERR="curl: (6) Could not resolve host"
    )
    assert r.code == 1
    assert "HTTP 000: no HTTP response" in r.out
    assert "Could not resolve host" in r.out
