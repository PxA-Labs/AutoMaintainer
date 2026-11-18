"""Tests of the Cloud Run Jobs sandbox backend (#158, #527).

No Google API is called. Three layers are covered:

- ``CloudRunJobSandbox`` against ``FakeClient``, an in-memory stand-in for the
  GCP client: the same cases as the Docker sandbox tests (copy-in, exit codes,
  output cap, timeout, cancellation, path escapes) plus object cleanup;
- the job's entrypoint script, run for real in a subprocess against a local
  HTTP server that plays Cloud Storage (``LocalJobClient``);
- ``GcpRestClient``'s requests, through ``httpx.MockTransport``.
"""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

from automaintainer._runtime import verification
from automaintainer.runners import cloud_run_job_sandbox as crj
from automaintainer.runners.base import (
    CommandTimeoutError,
    PathTraversalError,
    RunnerError,
)
from automaintainer.runners.cloud_run_job_sandbox import (
    ENTRYPOINT,
    CloudRunJobSandbox,
    CloudRunJobSandboxConfig,
    ExecutionStatus,
    GcpRestClient,
)
from automaintainer.runners.sandbox import SandboxBackend, load_sandbox_backend_factory

SETTINGS = (
    "SANDBOX_BACKEND",
    "CLOUD_RUN_SANDBOX_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "CLOUD_RUN_SANDBOX_REGION",
    "CLOUD_RUN_SANDBOX_JOB",
    "CLOUD_RUN_SANDBOX_BUCKET",
    "CLOUD_RUN_SANDBOX_SIGNER",
    "CLOUD_RUN_SANDBOX_START_SECONDS",
    "VERIFY_MAX_OUTPUT_BYTES",
    "VERIFY_ALLOW_LOCAL",
    "VERIFY_CHANGES",
)

HOST_SECRETS = {
    "GITHUB_TOKEN": "ghs_secret",
    "SUPABASE_SERVICE_KEY": "supabase_secret",
    "GROQ_API_KEY": "groq_secret",
}

CONFIG = CloudRunJobSandboxConfig(
    project="proj",
    region="us-central1",
    job="automaintainer-sandbox",
    bucket="proj-automaintainer-sandbox",
    signer="worker@proj.iam.gserviceaccount.com",
)

EXECUTION = (
    "projects/proj/locations/us-central1/jobs/automaintainer-sandbox/executions/e-1"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in HOST_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(crj, "POLL_INTERVAL", 0.01)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (root / ".automaintainer.yml").write_text(
        'test_command: ["python", "-m", "pytest", "-q"]\n', encoding="utf-8"
    )
    return root


def result_object(exit_code=0, body=b"", *, timed_out=False, total=None, error=None):
    header = {
        "exit": exit_code,
        "timed_out": timed_out,
        "total": len(body) if total is None else total,
        "error": error,
    }
    return json.dumps(header).encode() + b"\n" + body


class FakeClient:
    """In-memory stand-in for ``GcpRestClient``.

    ``behaviour(argv, workspace_tar) -> (result object or None, succeeded)``
    decides what the job "did"; ``hang`` keeps the execution running.
    """

    def __init__(
        self,
        behaviour=None,
        *,
        hang=False,
        run_error=None,
        upload_error=None,
        read_error=None,
        polls=2,
    ):
        self.behaviour = behaviour or (
            lambda argv, tar: (result_object(0, b"ok\n"), True)
        )
        self.hang = hang
        self.run_error = run_error
        self.upload_error = upload_error
        self.read_error = read_error
        self.polls = polls
        self.objects = {}
        self.uploaded = {}
        self.signed = []
        self.runs = []
        self.cancelled = []
        self.deleted = []
        self.events = []
        self.entered = self.exited = 0
        self._outcome = None

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        self.exited += 1

    async def upload_object(self, name, data):
        self.events.append("upload")
        if self.upload_error:
            raise self.upload_error
        self.objects[name] = self.uploaded[name] = data.read()

    async def sign_url(self, name, method, expires_seconds, headers=None):
        self.signed.append((name, method, expires_seconds, dict(headers or {})))
        return f"https://signed.example/{method}/{name}"

    def _signed_name(self, method):
        return next(n for n, m, _, _ in self.signed if m == method)

    async def run_job(self, args, env, task_timeout_seconds):
        self.events.append("run")
        self.runs.append(
            {"args": list(args), "env": dict(env), "timeout": task_timeout_seconds}
        )
        if self.run_error:
            raise self.run_error
        argv = list(args)[3:]
        tar = self.objects[self._signed_name("GET")]
        result, succeeded = self.behaviour(argv, tar)
        if result is not None:
            self.objects[self._signed_name("PUT")] = result
        self._outcome = succeeded
        return EXECUTION

    async def get_execution(self, execution):
        assert execution == EXECUTION
        self.events.append("poll")
        if self.hang or self.polls > 1:
            self.polls -= 1
            return ExecutionStatus(done=False, succeeded=False)
        return ExecutionStatus(
            done=True,
            succeeded=self._outcome,
            message="" if self._outcome else "Task failed: exit code 1",
        )

    async def cancel_execution(self, execution):
        self.events.append("cancel")
        self.cancelled.append(execution)

    async def read_object(self, name, max_bytes):
        self.events.append("read")
        if self.read_error:
            raise self.read_error
        data = self.objects.get(name)
        return None if data is None else data[:max_bytes]

    async def delete_object(self, name):
        self.events.append("delete")
        self.deleted.append(name)
        self.objects.pop(name, None)


def sandbox(repo, client, config=CONFIG):
    return CloudRunJobSandbox(repo, config, client=client)


# --- configuration and factory ------------------------------------------------


def _full_env(**overrides):
    env = {
        "GOOGLE_CLOUD_PROJECT": "proj",
        "CLOUD_RUN_SANDBOX_REGION": "us-central1",
        "CLOUD_RUN_SANDBOX_JOB": "automaintainer-sandbox",
        "CLOUD_RUN_SANDBOX_BUCKET": "proj-automaintainer-sandbox",
    }
    env.update(overrides)
    return env


def test_config_from_env_reads_every_setting():
    config = CloudRunJobSandboxConfig.from_env(
        _full_env(
            CLOUD_RUN_SANDBOX_SIGNER="worker@proj.iam.gserviceaccount.com",
            CLOUD_RUN_SANDBOX_START_SECONDS="15",
            VERIFY_MAX_OUTPUT_BYTES="4096",
        )
    )
    assert config == CloudRunJobSandboxConfig(
        project="proj",
        region="us-central1",
        job="automaintainer-sandbox",
        bucket="proj-automaintainer-sandbox",
        signer="worker@proj.iam.gserviceaccount.com",
        start_seconds=15,
        max_output_bytes=4096,
    )
    assert config.job_name == (
        "projects/proj/locations/us-central1/jobs/automaintainer-sandbox"
    )


def test_config_prefers_the_sandbox_project_and_floors_the_output_cap():
    config = CloudRunJobSandboxConfig.from_env(
        _full_env(CLOUD_RUN_SANDBOX_PROJECT="other", VERIFY_MAX_OUTPUT_BYTES="10")
    )
    assert config.project == "other"
    assert config.max_output_bytes == 1024
    assert config.signer is None
    assert config.start_seconds == crj.DEFAULT_START_SECONDS


@pytest.mark.parametrize(
    "name",
    [
        "GOOGLE_CLOUD_PROJECT",
        "CLOUD_RUN_SANDBOX_REGION",
        "CLOUD_RUN_SANDBOX_JOB",
        "CLOUD_RUN_SANDBOX_BUCKET",
    ],
)
def test_config_requires_the_job_and_bucket(name):
    env = _full_env()
    del env[name]
    with pytest.raises(
        RunnerError, match=name if name != "GOOGLE_CLOUD_PROJECT" else "PROJECT"
    ):
        CloudRunJobSandboxConfig.from_env(env)


@pytest.mark.parametrize(
    "name, value",
    [
        ("CLOUD_RUN_SANDBOX_JOB", "../jobs/x"),
        ("CLOUD_RUN_SANDBOX_JOB", "a/b"),
        ("CLOUD_RUN_SANDBOX_BUCKET", "Bad_Bucket!"),
        ("CLOUD_RUN_SANDBOX_REGION", "us central1"),
        ("GOOGLE_CLOUD_PROJECT", "p/../q"),
        ("CLOUD_RUN_SANDBOX_START_SECONDS", "-1"),
        ("CLOUD_RUN_SANDBOX_START_SECONDS", "soon"),
        ("CLOUD_RUN_SANDBOX_SIGNER", "not an email"),
    ],
)
def test_invalid_settings_are_refused(name, value):
    with pytest.raises(RunnerError, match=name):
        CloudRunJobSandboxConfig.from_env(_full_env(**{name: value}))


@pytest.mark.parametrize(
    "spec", ["cloud_run_job", "automaintainer.runners.cloud_run_job_sandbox:create"]
)
def test_factory_is_loadable_through_sandbox_backend(monkeypatch, repo, spec):
    for k, v in _full_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SANDBOX_BACKEND", spec)
    factory = load_sandbox_backend_factory()
    backend = factory(repo)
    assert isinstance(backend, CloudRunJobSandbox)
    assert isinstance(backend, SandboxBackend)
    assert backend.config.job == "automaintainer-sandbox"


def test_unknown_short_names_are_still_refused(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "cloud_run")
    with pytest.raises(RunnerError, match="package.module:factory"):
        load_sandbox_backend_factory()


# --- exec_command --------------------------------------------------------------


async def test_exec_runs_one_job_execution_with_per_run_overrides(repo):
    client = FakeClient(lambda argv, tar: (result_object(0, b"1 passed\n"), True))
    code, out, note = await sandbox(repo, client).exec_command(
        ["python", "-m", "pytest"], timeout=30
    )
    assert (code, out, note) == (0, "1 passed\n", "")
    [run] = client.runs
    # The entrypoint travels with the execution; the command follows it as
    # arguments, never interpolated into a script or a shell.
    assert run["args"] == ["-I", "-c", ENTRYPOINT, "python", "-m", "pytest"]
    assert run["env"] == {
        crj.INPUT_URL_ENV: f"https://signed.example/GET/{client.signed[0][0]}",
        crj.OUTPUT_URL_ENV: f"https://signed.example/PUT/{client.signed[1][0]}",
        crj.TIMEOUT_ENV: "30",
        crj.MAX_OUTPUT_ENV: str(CONFIG.max_output_bytes),
        crj.OUTPUT_RANGE_ENV: f"0,{CONFIG.max_output_bytes + crj.RESULT_HEADER_BYTES}",
    }
    # The platform kills the task even if the worker dies mid-run.
    assert run["timeout"] == 30 + CONFIG.start_seconds + crj.TASK_TIMEOUT_MARGIN
    assert client.entered == client.exited == 1


async def test_nothing_from_the_worker_environment_reaches_the_job(repo):
    client = FakeClient()
    await sandbox(repo, client).exec_command(["true"])
    sent = json.dumps(client.runs[0]) + json.dumps(client.signed)
    for value in HOST_SECRETS.values():
        assert value not in sent


async def test_signed_urls_are_short_lived_and_scoped_to_one_object(repo):
    client = FakeClient()
    await sandbox(repo, client).exec_command(["true"], timeout=40)
    (get_name, get, get_exp, get_headers), (put_name, put, put_exp, put_headers) = (
        client.signed
    )
    assert (get, put) == ("GET", "PUT")
    assert get_name != put_name
    assert get_exp == put_exp == 40 + CONFIG.start_seconds + crj.SIGNED_URL_MARGIN
    assert get_headers == {}
    # The upload cannot exceed the output cap (plus the result header).
    assert put_headers == {
        "x-goog-content-length-range": f"0,{CONFIG.max_output_bytes + crj.RESULT_HEADER_BYTES}"
    }


async def test_object_names_are_unguessable_and_unique_per_run(repo):
    prefixes = []
    for _ in range(2):
        client = FakeClient()
        await sandbox(repo, client).exec_command(["true"])
        names = [n for n, *_ in client.signed]
        prefix = names[0].rsplit("/", 1)[0]
        assert all(n.startswith(prefix + "/") for n in names)
        assert re.fullmatch(r"executions/[0-9a-f]{32}", prefix)
        prefixes.append(prefix)
    assert prefixes[0] != prefixes[1]


async def test_job_receives_a_copy_of_the_working_copy(repo):
    client = FakeClient()
    await sandbox(repo, client).exec_command(["true"])
    [data] = client.uploaded.values()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = sorted(tar.getnames())
        content = tar.extractfile("pkg/mod.py").read()
        members = tar.getmembers()
    assert names == [".automaintainer.yml", "pkg", "pkg/mod.py"]
    assert content == (repo / "pkg" / "mod.py").read_bytes()
    assert all((m.uid, m.gid, m.uname, m.gname) == (0, 0, "", "") for m in members)


def _can_symlink(tmp_path):
    try:
        (tmp_path / "probe").symlink_to(tmp_path)
        return True
    except (OSError, NotImplementedError):
        return False


async def test_symlinks_are_copied_as_links_not_followed(repo, tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("symlinks not permitted here")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (repo / "leak").symlink_to(secret)
    client = FakeClient()
    await sandbox(repo, client).exec_command(["true"])
    [data] = client.uploaded.values()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        assert tar.getmember("leak").issym()
    assert b"TOP SECRET" not in data


async def test_test_failure_exit_code_is_returned(repo):
    client = FakeClient(lambda argv, tar: (result_object(1, b"1 failed\n"), False))
    code, out, _ = await sandbox(repo, client).exec_command(["pytest"])
    assert (code, out) == (1, "1 failed\n")


async def test_output_is_capped_to_the_tail(repo):
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "max_output_bytes": 1024})
    body = b"c" * 1021 + b"END"
    client = FakeClient(lambda argv, tar: (result_object(1, body, total=3003), False))
    code, out, note = await sandbox(repo, client, config).exec_command(["x"])
    assert code == 1
    assert out.endswith("END") and len(out.encode()) == 1024
    assert "truncated" in note and "3003" in note


async def test_an_oversized_result_is_cut_to_the_cap(repo):
    # The result object is untrusted: never keep more than the cap.
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "max_output_bytes": 1024})
    client = FakeClient(lambda argv, tar: (result_object(0, b"z" * 5000), True))
    _, out, note = await sandbox(repo, client, config).exec_command(["x"])
    assert len(out.encode()) <= 1024
    assert "truncated" in note


async def test_objects_are_deleted_after_a_normal_run(repo):
    client = FakeClient()
    await sandbox(repo, client).exec_command(["true"])
    assert sorted(client.deleted) == sorted(n for n, *_ in client.signed)
    assert client.objects == {}
    assert client.cancelled == []  # finished: nothing to cancel


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"run_error": RunnerError("run denied")}, "run denied"),
        ({"upload_error": RunnerError("upload denied")}, "upload denied"),
        ({"read_error": RunnerError("read denied")}, "read denied"),
    ],
)
async def test_objects_are_deleted_when_a_step_fails(repo, kwargs, error):
    client = FakeClient(**kwargs)
    with pytest.raises(RunnerError, match=error):
        await sandbox(repo, client).exec_command(["true"])
    assert sorted(client.deleted) == sorted(n for n, *_ in client.signed)
    assert client.exited == 1


async def test_cleanup_failure_does_not_hide_the_result(repo):
    client = FakeClient(lambda argv, tar: (result_object(1, b"1 failed\n"), False))

    async def broken_delete(name):
        raise RunnerError("storage down")

    client.delete_object = broken_delete
    code, out, _ = await sandbox(repo, client).exec_command(["pytest"])
    assert (code, out) == (1, "1 failed\n")


async def test_timeout_cancels_the_execution_and_deletes_objects(repo):
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "start_seconds": 0})
    client = FakeClient(hang=True)
    with pytest.raises(CommandTimeoutError, match="did not finish"):
        await sandbox(repo, client, config).exec_command(["sleep", "999"], timeout=0.3)
    assert client.cancelled == [EXECUTION]
    assert sorted(client.deleted) == sorted(n for n, *_ in client.signed)
    assert client.events.index("cancel") < client.events.index("delete")


async def test_start_allowance_extends_the_worker_deadline(repo):
    # A slow start (cold container) is not counted against the test's own
    # timeout, up to start_seconds.
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "start_seconds": 2})
    client = FakeClient(polls=30)  # about 0.3 s of polling at 0.01 s
    code, _, _ = await sandbox(repo, client, config).exec_command(["true"], timeout=0.1)
    assert code == 0


async def test_cancellation_cancels_the_execution_and_deletes_objects(repo):
    client = FakeClient(hang=True)
    task = asyncio.ensure_future(
        sandbox(repo, client).exec_command(["sleep", "999"], timeout=60)
    )
    for _ in range(200):
        if "poll" in client.events:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.cancelled == [EXECUTION]
    assert sorted(client.deleted) == sorted(n for n, *_ in client.signed)


async def test_cancellation_while_starting_still_cancels_the_execution(repo):
    # The outer wait_for fires while run_job's request is in flight: the
    # execution it creates must still be cancelled once its name is known.
    client = FakeClient(hang=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_run_job(args, env, task_timeout_seconds):
        # Cleanup may already have deleted the input: a real job reads it
        # only later, so this stand-in does not read it at all.
        started.set()
        await release.wait()
        client.events.append("run")
        return EXECUTION

    client.run_job = slow_run_job
    task = asyncio.ensure_future(
        sandbox(repo, client).exec_command(["sleep", "999"], timeout=60)
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.cancelled == [EXECUTION]
    assert sorted(client.deleted) == sorted(n for n, *_ in client.signed)
    assert client.exited == 1


async def test_cleanup_is_not_cut_short_by_a_second_cancellation(repo):
    # verification.py's own wait_for may fire while cleanup is running; the
    # cancel and both deletes still happen, and the client is closed after.
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "start_seconds": 0})
    client = FakeClient(hang=True)
    in_cleanup = asyncio.Event()
    real_cancel = client.cancel_execution

    async def slow_cancel(execution):
        in_cleanup.set()
        await asyncio.sleep(0.2)
        await real_cancel(execution)

    client.cancel_execution = slow_cancel
    task = asyncio.ensure_future(
        sandbox(repo, client, config).exec_command(["sleep", "999"], timeout=0.1)
    )
    await asyncio.wait_for(in_cleanup.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if client.exited:
            break
        await asyncio.sleep(0.01)
    assert client.cancelled == [EXECUTION]
    assert sorted(client.deleted) == sorted(n for n, *_ in client.signed)
    assert client.exited == 1


async def test_cleanup_has_one_budget_for_all_its_calls(monkeypatch, repo):
    # Every cleanup call hangs: cleanup still ends after one CLEANUP_TIMEOUT,
    # not one per call, and the input archive is deleted alongside the cancel.
    monkeypatch.setattr(crj, "CLEANUP_TIMEOUT", 0.5)
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "start_seconds": 0})
    client = FakeClient(hang=True)
    real_delete = client.delete_object

    async def hang(*args):
        await asyncio.sleep(3600)

    async def delete(name):
        if name.endswith("workspace.tar"):
            return await real_delete(name)
        await hang()

    client.cancel_execution = hang
    client.delete_object = delete
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(CommandTimeoutError):
        await sandbox(repo, client, config).exec_command(["sleep", "999"], timeout=0.1)
    # 0.1 s timeout plus one 0.5 s budget; one budget per call would be 1.1 s.
    assert loop.time() - started < 0.95
    assert client.deleted == [client._signed_name("GET")]
    assert client.exited == 1


def test_start_allowance_leaves_room_for_cleanup_within_verifications_grace():
    # verification.py waits timeout + 30 s; the worker's own deadline plus
    # cleanup must fit in that, or cleanup is the part that gets cut off.
    assert crj.MAX_START_SECONDS + crj.CLEANUP_TIMEOUT < 30
    with pytest.raises(RunnerError, match="CLOUD_RUN_SANDBOX_START_SECONDS"):
        CloudRunJobSandboxConfig.from_env(
            _full_env(CLOUD_RUN_SANDBOX_START_SECONDS=str(crj.MAX_START_SECONDS + 1))
        )


async def test_timeout_inside_the_job_is_a_timeout(repo):
    client = FakeClient(
        lambda argv, tar: (result_object(124, b"started\n", timed_out=True), False)
    )
    with pytest.raises(CommandTimeoutError, match="did not finish"):
        await sandbox(repo, client).exec_command(["sleep", "999"], timeout=5)


async def test_setup_failure_in_the_job_is_a_runner_error(repo):
    client = FakeClient(
        lambda argv, tar: (
            result_object(125, error="could not unpack the working copy: bad"),
            False,
        )
    )
    with pytest.raises(RunnerError, match="could not unpack"):
        await sandbox(repo, client).exec_command(["true"])


async def test_an_execution_without_a_result_is_a_runner_error(repo):
    client = FakeClient(lambda argv, tar: (None, False))
    with pytest.raises(RunnerError, match="exit code 1"):
        await sandbox(repo, client).exec_command(["true"])


async def test_a_malformed_result_is_a_runner_error(repo):
    client = FakeClient(lambda argv, tar: (b"exit 0, trust me\n", True))
    with pytest.raises(RunnerError, match="malformed"):
        await sandbox(repo, client).exec_command(["true"])


@pytest.mark.parametrize("exit_code, succeeded", [(0, False), (1, True)])
async def test_a_result_contradicting_the_execution_is_refused(
    repo, exit_code, succeeded
):
    # The task's own outcome (exit status seen by Cloud Run) must agree with
    # the uploaded result, so a test cannot report success by rewriting it.
    client = FakeClient(lambda argv, tar: (result_object(exit_code, b""), succeeded))
    with pytest.raises(RunnerError, match="contradicts"):
        await sandbox(repo, client).exec_command(["true"])


async def test_packing_time_counts_against_the_timeout(monkeypatch, repo):
    def slow_archive(src):
        import time

        time.sleep(0.5)
        return io.BytesIO(b"")

    monkeypatch.setattr(crj, "_archive_workspace", slow_archive)
    client = FakeClient()
    with pytest.raises(CommandTimeoutError, match="pack"):
        await sandbox(repo, client).exec_command(["true"], timeout=0.1)
    assert client.runs == [] and client.uploaded == {}


async def test_packing_failure_is_a_runner_error(monkeypatch, repo):
    def broken(src):
        raise PermissionError("denied")

    monkeypatch.setattr(crj, "_archive_workspace", broken)
    client = FakeClient()
    with pytest.raises(RunnerError, match="pack the working copy"):
        await sandbox(repo, client).exec_command(["true"])
    assert client.runs == []


async def test_invalid_commands_are_refused_before_any_call(repo):
    client = FakeClient()
    for bad in ([], "", [1, 2]):
        with pytest.raises(RunnerError):
            await sandbox(repo, client).exec_command(bad)
    assert client.events == []


async def test_verify_working_copy_through_the_cloud_run_backend(monkeypatch, repo):
    client = FakeClient(
        lambda argv, tar: (result_object(0, b"z" * 5000 + b"\n1 passed\n"), True)
    )
    monkeypatch.setenv("VERIFY_MAX_OUTPUT_BYTES", "1024")
    result = await verification.verify_working_copy(
        repo, sandbox_factory=lambda p: sandbox(p, client)
    )
    assert result.status == "passed"
    assert result.runner == "sandbox"
    assert result.truncated is True
    assert client.runs[0]["args"][3:] == ["python", "-m", "pytest", "-q"]


async def test_verify_working_copy_reports_a_job_timeout_as_error(repo):
    (repo / ".automaintainer.yml").write_text(
        'test_command: ["sleep", "99"]\ntimeout_seconds: 1\n', encoding="utf-8"
    )
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "start_seconds": 0})
    client = FakeClient(hang=True)
    result = await verification.verify_working_copy(
        repo, sandbox_factory=lambda p: sandbox(p, client, config)
    )
    assert result.status == "error"
    assert "timed out" in result.summary
    assert client.cancelled == [EXECUTION]


# --- file operations, diff, push ----------------------------------------------


async def test_file_operations_are_confined_to_the_working_copy(repo):
    box = sandbox(repo, FakeClient())
    await box.write_file("pkg/new.py", "Y = 2\n")
    assert await box.read_file("pkg/new.py") == "Y = 2\n"
    assert "pkg/new.py" in await box.list_files("*.py")
    await box.delete_file("pkg/new.py")
    assert not (repo / "pkg" / "new.py").exists()
    for bad in ("../outside.txt", "pkg/../../outside.txt"):
        with pytest.raises(PathTraversalError):
            await box.write_file(bad, "x")
        with pytest.raises(PathTraversalError):
            await box.read_file(bad)
        with pytest.raises(PathTraversalError):
            await box.delete_file(bad)
    assert not (repo.parent / "outside.txt").exists()


async def test_commit_and_push_is_not_supported(repo):
    with pytest.raises(NotImplementedError, match="never commits or pushes"):
        await sandbox(repo, FakeClient()).commit_and_push("b", "m", dry_run=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_get_git_diff_reports_working_copy_changes(repo):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    (repo / "pkg" / "mod.py").write_text("X = 2\n", encoding="utf-8")
    diff = await sandbox(repo, FakeClient()).get_git_diff()
    assert "-X = 1" in diff and "+X = 2" in diff


# --- the job's entrypoint, run for real ------------------------------------------


class _StorageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = self.server.objects.get(unquote(urlsplit(self.path).path[1:]))
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        length = int(self.headers["Content-Length"])
        name = unquote(urlsplit(self.path).path[1:])
        self.server.objects[name] = self.rfile.read(length)
        # Header names are case-insensitive (urllib sends "X-goog-...").
        self.server.put_headers[name] = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(200)
        self.end_headers()


@pytest.fixture
def storage():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StorageHandler)
    server.objects = {}
    server.put_headers = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


class LocalJobClient(FakeClient):
    """Runs the real entrypoint in a local subprocess, the way the Cloud Run
    job would, with the local HTTP server standing in for Cloud Storage."""

    def __init__(self, server):
        super().__init__()
        self.server = server
        self.proc = None

    async def upload_object(self, name, data):
        self.server.objects[name] = self.uploaded[name] = data.read()

    async def sign_url(self, name, method, expires_seconds, headers=None):
        self.signed.append((name, method, expires_seconds, dict(headers or {})))
        return f"http://127.0.0.1:{self.server.server_port}/{name}"

    async def run_job(self, args, env, task_timeout_seconds):
        self.runs.append({"args": list(args), "env": dict(env)})
        # Only what Cloud Run itself would provide, plus the overrides.
        base = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, *args, env={**base, **env}
        )
        return EXECUTION

    async def get_execution(self, execution):
        if self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), 0.05)
            except asyncio.TimeoutError:
                return ExecutionStatus(done=False, succeeded=False)
        return ExecutionStatus(done=True, succeeded=self.proc.returncode == 0)

    async def cancel_execution(self, execution):
        self.cancelled.append(execution)
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()

    async def read_object(self, name, max_bytes):
        data = self.server.objects.get(name)
        return None if data is None else data[:max_bytes]

    async def delete_object(self, name):
        self.deleted.append(name)
        self.server.objects.pop(name, None)


PY = sys.executable


async def test_entrypoint_runs_the_command_in_a_copy_with_a_clean_environment(
    repo, storage
):
    client = LocalJobClient(storage)
    script = (
        "import os, pathlib; "
        "print(pathlib.Path('pkg/mod.py').read_text().strip()); "
        "print(sorted(k for k in os.environ if k.startswith('AM_') or k in "
        "('GITHUB_TOKEN', 'GROQ_API_KEY'))); "
        "pathlib.Path('pkg/mod.py').write_text('changed'); "
        "print(os.environ['CI'], os.environ['HOME'])"
    )
    code, out, note = await sandbox(repo, client).exec_command([PY, "-c", script])
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0] == "X = 1"
    assert lines[1] == "[]"  # neither the signed URLs nor host secrets
    assert lines[2].split()[0] == "true"
    assert note == ""
    # The host clone is untouched; the objects are gone.
    assert (repo / "pkg" / "mod.py").read_text() == "X = 1\n"
    assert storage.objects == {}


async def test_entrypoint_reports_a_failing_exit_code(repo, storage):
    client = LocalJobClient(storage)
    code, out, _ = await sandbox(repo, client).exec_command(
        [PY, "-c", "import sys; print('1 failed'); sys.exit(3)"]
    )
    assert (code, out.strip()) == (3, "1 failed")


async def test_entrypoint_missing_program_is_a_test_failure_like_docker(repo, storage):
    client = LocalJobClient(storage)
    code, out, _ = await sandbox(repo, client).exec_command(["no-such-program-xyz"])
    assert code == 127
    assert "no-such-program-xyz" in out


async def test_entrypoint_caps_output_to_the_tail(repo, storage):
    config = CloudRunJobSandboxConfig(**{**CONFIG.__dict__, "max_output_bytes": 1024})
    client = LocalJobClient(storage)
    code, out, note = await sandbox(repo, client, config).exec_command(
        [PY, "-c", "import sys; sys.stdout.write('a' * 5000 + 'END')"]
    )
    assert code == 0
    assert out.endswith("END") and len(out.encode()) == 1024
    assert "5003" in note
    [headers] = [h for h in storage.put_headers.values()]
    assert (
        headers["x-goog-content-length-range"] == f"0,{1024 + crj.RESULT_HEADER_BYTES}"
    )


async def test_entrypoint_enforces_the_timeout_inside_the_job(repo, storage):
    client = LocalJobClient(storage)
    with pytest.raises(CommandTimeoutError):
        await sandbox(repo, client).exec_command(
            [PY, "-c", "import time; print('started', flush=True); time.sleep(60)"],
            timeout=1,
        )
    assert client.cancelled == []  # the job ended by itself
    assert storage.objects == {}


async def test_entrypoint_refuses_an_archive_that_escapes_the_workspace(
    tmp_path, storage
):
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w") as tar:
        data = b"owned"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    storage.objects["in.tar"] = evil.getvalue()
    port = storage.server_port
    env = {
        **{k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ},
        crj.INPUT_URL_ENV: f"http://127.0.0.1:{port}/in.tar",
        crj.OUTPUT_URL_ENV: f"http://127.0.0.1:{port}/out",
        crj.TIMEOUT_ENV: "10",
        crj.MAX_OUTPUT_ENV: "1024",
        crj.OUTPUT_RANGE_ENV: "0,2048",
        "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
    }
    proc = subprocess.run(
        [PY, "-I", "-c", ENTRYPOINT, PY, "-c", "print('ran')"],
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == crj.SETUP_ERROR_EXIT
    header = json.loads(storage.objects["out"].split(b"\n", 1)[0])
    assert header["exit"] == crj.SETUP_ERROR_EXIT and "unpack" in header["error"]
    assert not list(tmp_path.rglob("escaped.txt"))


# --- GcpRestClient requests ------------------------------------------------------


class Recorder:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        for (method, pattern), response in self.responses.items():
            if request.method == method and re.search(pattern, str(request.url)):
                return response(request) if callable(response) else response
        return httpx.Response(500, json={"error": "unexpected " + str(request.url)})


def rest_client(recorder, **kwargs):
    async def token():
        return "tok"

    return GcpRestClient(
        CONFIG,
        transport=httpx.MockTransport(recorder),
        token=token,
        **kwargs,
    )


async def test_rest_client_runs_the_job_with_overrides():
    op = {"name": "operations/1", "metadata": {"name": EXECUTION}}
    rec = Recorder(
        {
            (
                "POST",
                r"/v2/projects/proj/locations/us-central1/jobs/automaintainer-sandbox:run$",
            ): httpx.Response(200, json=op)
        }
    )
    async with rest_client(rec) as client:
        execution = await client.run_job(["-I", "-c", "s", "pytest"], {"A": "1"}, 90)
    assert execution == EXECUTION
    [req] = rec.requests
    assert req.url.host == "run.googleapis.com"
    assert req.headers["authorization"] == "Bearer tok"
    assert json.loads(req.content) == {
        "overrides": {
            "containerOverrides": [
                {
                    "args": ["-I", "-c", "s", "pytest"],
                    "env": [{"name": "A", "value": "1"}],
                }
            ],
            "taskCount": 1,
            "timeout": "90s",
        }
    }


async def test_rest_client_reads_execution_status():
    running = {"name": EXECUTION, "taskCount": 1}
    failed = {
        "name": EXECUTION,
        "completionTime": "2026-01-01T00:00:00Z",
        "failedCount": 1,
        "conditions": [
            {
                "type": "Completed",
                "state": "CONDITION_FAILED",
                "message": "Task failed: exit code 1",
            }
        ],
    }
    passed = {
        "name": EXECUTION,
        "completionTime": "2026-01-01T00:00:00Z",
        "succeededCount": 1,
    }
    for body, expected in (
        (running, ExecutionStatus(done=False, succeeded=False)),
        (
            failed,
            ExecutionStatus(
                done=True, succeeded=False, message="Task failed: exit code 1"
            ),
        ),
        (passed, ExecutionStatus(done=True, succeeded=True)),
    ):
        rec = Recorder(
            {("GET", "/v2/" + EXECUTION + "$"): httpx.Response(200, json=body)}
        )
        async with rest_client(rec) as client:
            assert await client.get_execution(EXECUTION) == expected


async def test_rest_client_cancels_and_tolerates_finished_executions():
    rec = Recorder({("POST", ":cancel$"): httpx.Response(200, json={})})
    async with rest_client(rec) as client:
        await client.cancel_execution(EXECUTION)
    assert str(rec.requests[0].url).endswith(EXECUTION + ":cancel")
    rec = Recorder(
        {
            ("POST", ":cancel$"): httpx.Response(
                400, json={"error": {"status": "FAILED_PRECONDITION"}}
            )
        }
    )
    async with rest_client(rec) as client:
        await client.cancel_execution(EXECUTION)  # already finished: no error


async def test_rest_client_uploads_reads_and_deletes_objects():
    stored = {}

    def upload(request):
        stored["body"] = request.content
        stored["length"] = request.headers["content-length"]
        return httpx.Response(200, json={})

    rec = Recorder(
        {
            (
                "POST",
                r"/upload/storage/v1/b/proj-automaintainer-sandbox/o\?uploadType=media&name=executions%2Fab%2Fworkspace.tar$",
            ): upload,
            (
                "GET",
                r"/storage/v1/b/proj-automaintainer-sandbox/o/executions%2Fab%2Foutput\?alt=media$",
            ): httpx.Response(200, content=b"x" * 100),
            ("GET", r"/o/executions%2Fab%2Fmissing\?alt=media$"): httpx.Response(404),
            ("DELETE", r"/o/executions%2Fab%2F"): httpx.Response(404),
        }
    )
    async with rest_client(rec) as client:
        await client.upload_object(
            "executions/ab/workspace.tar", io.BytesIO(b"tar" * 10)
        )
        assert await client.read_object("executions/ab/output", 10) == b"x" * 10
        assert await client.read_object("executions/ab/missing", 10) is None
        await client.delete_object("executions/ab/output")  # 404 is fine
    assert stored == {"body": b"tar" * 10, "length": "30"}


async def test_rest_client_errors_are_runner_errors_without_the_token():
    rec = Recorder(
        {("POST", ":run$"): httpx.Response(403, json={"error": {"message": "denied"}})}
    )
    async with rest_client(rec) as client:
        with pytest.raises(RunnerError, match="403") as info:
            await client.run_job(["x"], {}, 10)
    assert "tok" not in str(info.value)


async def test_rest_client_signs_v4_urls_through_iam_sign_blob():
    signed = {}

    def sign_blob(request):
        payload = base64.b64decode(json.loads(request.content)["payload"])
        signed["payload"] = payload.decode()
        return httpx.Response(
            200,
            json={"keyId": "k", "signedBlob": base64.b64encode(b"\x01\xff").decode()},
        )

    rec = Recorder(
        {
            (
                "POST",
                r"iamcredentials\.googleapis\.com/v1/projects/-/serviceAccounts/worker@proj\.iam\.gserviceaccount\.com:signBlob$",
            ): sign_blob
        }
    )
    when = datetime(2026, 11, 18, 10, 30, 0, tzinfo=timezone.utc)
    async with rest_client(rec, now=lambda: when) as client:
        url = await client.sign_url(
            "executions/ab/output",
            "PUT",
            300,
            {"x-goog-content-length-range": "0,2048"},
        )
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "storage.googleapis.com",
        "/proj-automaintainer-sandbox/executions/ab/output",
    )
    query = parse_qs(parts.query)
    assert query["X-Goog-Algorithm"] == ["GOOG4-RSA-SHA256"]
    assert query["X-Goog-Credential"] == [
        "worker@proj.iam.gserviceaccount.com/20261118/auto/storage/goog4_request"
    ]
    assert query["X-Goog-Date"] == ["20261118T103000Z"]
    assert query["X-Goog-Expires"] == ["300"]
    assert query["X-Goog-SignedHeaders"] == ["host;x-goog-content-length-range"]
    assert query["X-Goog-Signature"] == ["01ff"]
    # The string to sign is the V4 one, over the canonical request.
    canonical_query = parts.query.rsplit("&X-Goog-Signature=", 1)[0]
    canonical = "\n".join(
        [
            "PUT",
            "/proj-automaintainer-sandbox/executions/ab/output",
            canonical_query,
            "host:storage.googleapis.com\nx-goog-content-length-range:0,2048\n",
            "host;x-goog-content-length-range",
            "UNSIGNED-PAYLOAD",
        ]
    )
    assert signed["payload"] == "\n".join(
        [
            "GOOG4-RSA-SHA256",
            "20261118T103000Z",
            "20261118/auto/storage/goog4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    # Query keys are sorted, as the canonical form requires.
    keys = [kv.split("=", 1)[0] for kv in canonical_query.split("&")]
    assert keys == sorted(keys)


async def test_rest_client_refuses_expiry_beyond_seven_days():
    async with rest_client(Recorder({})) as client:
        with pytest.raises(RunnerError, match="expir"):
            await client.sign_url("o", "GET", 604801)
