"""Unit tests for runners/docker_sandbox.py. The docker CLI is mocked: these
run without Docker. Real containers are exercised by
test_docker_sandbox_integration.py (CI workflow sandbox.yml)."""

import asyncio
import io
import os
import shutil
import subprocess
import tarfile

import pytest

import verification
from runners import docker_sandbox
from runners.base import CommandTimeoutError, PathTraversalError, RunnerError
from runners.docker_sandbox import (
    SANDBOX_ENV,
    SETUP_SCRIPT,
    DockerSandbox,
    DockerSandboxConfig,
    docker_run_argv,
)
from runners.sandbox import SandboxBackend

SETTINGS = (
    "SANDBOX_IMAGE",
    "SANDBOX_NETWORK",
    "SANDBOX_MEMORY",
    "SANDBOX_CPUS",
    "SANDBOX_PIDS_LIMIT",
    "SANDBOX_TMPFS_SIZE",
    "SANDBOX_WORKSPACE_SIZE",
    "SANDBOX_USER",
    "SANDBOX_RUNTIME",
    "SANDBOX_DOCKER",
    "SANDBOX_BACKEND",
    "VERIFY_MAX_OUTPUT_BYTES",
    "VERIFY_ALLOW_LOCAL",
    "VERIFY_CHANGES",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    # Secrets that must never reach the docker CLI or the container.
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "supabase_secret")
    monkeypatch.setenv("GROQ_API_KEY", "groq_secret")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (root / ".automaintainer.yml").write_text(
        'test_command: ["python", "-m", "pytest", "-q"]\n', encoding="utf-8"
    )
    return root


CONFIG = DockerSandboxConfig()


class FakeStdin:
    def __init__(self, events, broken=False):
        self.data = bytearray()
        self.closed = False
        self._events = events
        self._broken = broken

    def write(self, chunk):
        if self._broken:
            raise BrokenPipeError("container exited")
        self.data.extend(chunk)

    async def drain(self):
        pass

    def close(self):
        self.closed = True
        self._events.append("stdin-closed")

    async def wait_closed(self):
        pass


class FakeStream:
    def __init__(self, chunks, hang=False, error=None):
        self._chunks = list(chunks)
        self._hang = hang
        self._error = error

    async def read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._error:
            raise self._error
        if self._hang:
            await asyncio.sleep(3600)
        return b""


class FakeProc:
    def __init__(
        self,
        chunks=(),
        returncode=0,
        hang=False,
        error=None,
        broken_stdin=False,
        events=None,
    ):
        self.events = [] if events is None else events
        self.stdin = FakeStdin(self.events, broken=broken_stdin)
        self.stdout = FakeStream(chunks, hang=hang, error=error)
        self._final = returncode
        self.returncode = None
        self.pid = 4242
        self.killed = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._final
        return self.returncode

    def kill(self):
        self.killed = True
        self.events.append("kill")


class FakeDocker:
    """Records docker CLI invocations; ``run`` answers `docker run`."""

    def __init__(self, run_proc=None, start_error=None, rm_codes=(0,)):
        self.calls = []
        self.events = []
        self.run_proc = run_proc or FakeProc([b"ok\n"])
        self.run_proc.events = self.events
        self.run_proc.stdin._events = self.events
        self.start_error = start_error
        self.rm_codes = list(rm_codes)

    async def __call__(self, *argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[1] == "run":
            if self.start_error:
                raise self.start_error
            return self.run_proc
        self.events.append(argv[1])
        code = self.rm_codes.pop(0) if len(self.rm_codes) > 1 else self.rm_codes[0]
        return FakeProc(returncode=code)

    @property
    def run_argv(self):
        return next(a for a, _ in self.calls if a[1] == "run")

    @property
    def run_kwargs(self):
        return next(k for a, k in self.calls if a[1] == "run")

    @property
    def rm_calls(self):
        return [a for a, _ in self.calls if a[1] == "rm"]

    def archive(self):
        """The tar archive the container received on stdin."""
        return tarfile.open(fileobj=io.BytesIO(bytes(self.run_proc.stdin.data)))


@pytest.fixture
def fake_docker(monkeypatch):
    def install(**kwargs):
        fake = FakeDocker(**kwargs)
        monkeypatch.setattr(
            docker_sandbox.asyncio, "create_subprocess_exec", fake, raising=True
        )
        monkeypatch.setattr(docker_sandbox.shutil, "which", lambda name: None)
        monkeypatch.setattr(docker_sandbox, "REMOVE_RETRY_DELAY", 0)
        return fake

    return install


# --- configuration and argv ---------------------------------------------------


def test_docker_run_argv_is_exact_and_hardened():
    argv = docker_run_argv(CONFIG, "automaintainer-sandbox-abc", ["python", "-V"])
    assert argv == [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--name=automaintainer-sandbox-abc",
        "--network=none",
        "--read-only",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=256m",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=1g",
        "--memory-swap=1g",
        "--cpus=1",
        "--user=65534:65534",
        "--workdir=/workspace",
        "--tmpfs=/workspace:rw,exec,nosuid,nodev,mode=1777,size=1g",
        "--entrypoint=/bin/sh",
        "--env=HOME=/tmp",
        "--env=TMPDIR=/tmp",
        "--env=CI=true",
        "--env=NO_COLOR=1",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=PYTHONUNBUFFERED=1",
        "--env=GIT_TERMINAL_PROMPT=0",
        "--env=GIT_CONFIG_COUNT=1",
        "--env=GIT_CONFIG_KEY_0=safe.directory",
        "--env=GIT_CONFIG_VALUE_0=/workspace",
        "python:3.12-slim",
        "-c",
        'tar -xf - -C /workspace || exit 125; exec "$@" </dev/null',
        "automaintainer-sandbox",
        "python",
        "-V",
    ]


def test_argv_has_no_bind_mount_or_volume():
    """The daemon may be remote or outside the worker's container: it cannot
    resolve a worker path, so the copy must not be passed as a mount."""
    argv = docker_run_argv(CONFIG, "n", ["true"])
    for a in argv:
        assert not a.startswith(("--mount", "--volume", "-v")), a
        assert "bind" not in a


def test_config_from_env_applies_overrides_and_runtime(monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGE", "ghcr.io/acme/tests@sha256:" + "a" * 64)
    monkeypatch.setenv("SANDBOX_NETWORK", "pypi-mirror")
    monkeypatch.setenv("SANDBOX_MEMORY", "2g")
    monkeypatch.setenv("SANDBOX_CPUS", "1.5")
    monkeypatch.setenv("SANDBOX_PIDS_LIMIT", "64")
    monkeypatch.setenv("SANDBOX_WORKSPACE_SIZE", "512m")
    monkeypatch.setenv("SANDBOX_USER", "1000:1000")
    monkeypatch.setenv("SANDBOX_RUNTIME", "runsc")
    config = DockerSandboxConfig.from_env()
    argv = docker_run_argv(config, "n", ["true"])
    for flag in (
        "--network=pypi-mirror",
        "--memory=2g",
        "--memory-swap=2g",
        "--cpus=1.5",
        "--pids-limit=64",
        "--tmpfs=/workspace:rw,exec,nosuid,nodev,mode=1777,size=512m",
        "--user=1000:1000",
        "--runtime=runsc",
    ):
        assert flag in argv
    image = "ghcr.io/acme/tests@sha256:" + "a" * 64
    assert argv[argv.index(image) :] == [
        image,
        "-c",
        SETUP_SCRIPT,
        "automaintainer-sandbox",
        "true",
    ]


def test_no_runtime_flag_by_default():
    assert not any(
        a.startswith("--runtime") for a in docker_run_argv(CONFIG, "n", ["x"])
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("SANDBOX_NETWORK", "host"),
        ("SANDBOX_NETWORK", "HOST"),
        ("SANDBOX_NETWORK", "container:abc"),
        ("SANDBOX_MEMORY", "1g --privileged"),
        ("SANDBOX_MEMORY", "0"),
        ("SANDBOX_MEMORY", "0g"),
        ("SANDBOX_CPUS", "all"),
        ("SANDBOX_CPUS", "0"),
        ("SANDBOX_CPUS", "0.0"),
        ("SANDBOX_PIDS_LIMIT", "0"),
        ("SANDBOX_PIDS_LIMIT", "-1"),
        ("SANDBOX_TMPFS_SIZE", "1g,exec"),
        ("SANDBOX_TMPFS_SIZE", "0m"),
        ("SANDBOX_WORKSPACE_SIZE", "0"),
        ("SANDBOX_WORKSPACE_SIZE", "1g,uid=0"),
        ("SANDBOX_USER", "0"),
        ("SANDBOX_USER", "0:0"),
        ("SANDBOX_USER", "root"),
        ("SANDBOX_IMAGE", "--privileged"),
        ("SANDBOX_RUNTIME", "runsc --privileged"),
    ],
)
def test_invalid_settings_are_refused(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RunnerError):
        DockerSandboxConfig.from_env()


def test_default_user_is_nobody():
    assert DockerSandboxConfig.from_env().user == "65534:65534"


def test_cli_env_keeps_daemon_access_variables_only():
    env = docker_sandbox._cli_env(
        {
            "PATH": "/usr/bin",
            "DOCKER_HOST": "ssh://sandbox@host",
            "SSH_AUTH_SOCK": "/run/agent.sock",
            "HTTPS_PROXY": "http://proxy:3128",
            "no_proxy": "localhost",
            "GITHUB_TOKEN": "ghs_secret",
        }
    )
    assert env == {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "ssh://sandbox@host",
        "SSH_AUTH_SOCK": "/run/agent.sock",
        "HTTPS_PROXY": "http://proxy:3128",
        "no_proxy": "localhost",
    }


def test_factory_is_loadable_through_sandbox_backend(monkeypatch, repo):
    monkeypatch.setenv("SANDBOX_BACKEND", "runners.docker_sandbox:create")
    factory = verification.load_sandbox_backend_factory()
    backend = factory(repo)
    assert isinstance(backend, DockerSandbox)
    assert isinstance(backend, SandboxBackend)


# --- exec_command -------------------------------------------------------------


async def test_exec_runs_hardened_container_without_host_env(fake_docker, repo):
    fake = fake_docker(run_proc=FakeProc([b"1 passed\n"], returncode=0))
    code, out, err = await DockerSandbox(repo, CONFIG).exec_command(
        ["python", "-m", "pytest"], timeout=30
    )
    assert (code, out, err) == (0, "1 passed\n", "")
    argv = fake.run_argv
    for flag in (
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--user=65534:65534",
    ):
        assert flag in argv
    assert argv[-3:] == ["python", "-m", "pytest"]
    # Container env is exactly the explicit minimal set ...
    envs = [a[len("--env=") :] for a in argv if a.startswith("--env=")]
    assert envs == [f"{k}={v}" for k, v in SANDBOX_ENV.items()]
    # ... and no secret reaches the argv or the CLI process.
    joined = "\n".join(argv)
    for secret in ("ghs_secret", "supabase_secret", "groq_secret"):
        assert secret not in joined
    cli_env = fake.run_kwargs["env"]
    assert not {"GITHUB_TOKEN", "SUPABASE_SERVICE_KEY", "GROQ_API_KEY"} & set(cli_env)
    # The copy goes in on stdin, which is closed afterwards.
    assert fake.run_kwargs["stdin"] == asyncio.subprocess.PIPE
    assert fake.run_proc.stdin.closed
    # No host path is handed to the daemon.
    assert str(repo) not in joined
    # Normal exit: --rm cleans up, no forced removal needed.
    assert fake.rm_calls == []


async def test_container_names_are_unique_per_run(fake_docker, repo):
    names = []
    for _ in range(2):
        fake = fake_docker()
        await DockerSandbox(repo, CONFIG).exec_command(["true"])
        names.append(next(a for a in fake.run_argv if a.startswith("--name=")))
    assert names[0] != names[1]
    assert all(n.startswith("--name=automaintainer-sandbox-") for n in names)


async def test_container_receives_a_copy_of_the_working_copy(fake_docker, repo):
    fake = fake_docker()
    await DockerSandbox(repo, CONFIG).exec_command(["true"])
    with fake.archive() as tar:
        names = sorted(tar.getnames())
        content = tar.extractfile("pkg/mod.py").read()
        members = tar.getmembers()
    # Children of the root only (never "." itself), so unpacking does not
    # try to chmod the root-owned /workspace.
    assert names == [".automaintainer.yml", "pkg", "pkg/mod.py"]
    assert content == (repo / "pkg" / "mod.py").read_bytes()
    # Host account details are not carried into the container.
    assert all((m.uid, m.gid, m.uname, m.gname) == (0, 0, "", "") for m in members)


def _can_symlink(tmp_path):
    try:
        (tmp_path / "probe").symlink_to(tmp_path)
        return True
    except (OSError, NotImplementedError):
        return False


async def test_symlinks_are_copied_as_links_not_followed(fake_docker, repo, tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("symlinks not permitted here")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (repo / "leak").symlink_to(secret)
    fake = fake_docker()
    await DockerSandbox(repo, CONFIG).exec_command(["true"])
    with fake.archive() as tar:
        member = tar.getmember("leak")
        assert member.issym()
        assert b"TOP SECRET" not in bytes(fake.run_proc.stdin.data)


async def test_timeout_kills_cli_before_removing_container(fake_docker, repo):
    proc = FakeProc([b"started\n"], hang=True)
    fake = fake_docker(run_proc=proc)
    with pytest.raises(CommandTimeoutError):
        await DockerSandbox(repo, CONFIG).exec_command(["sleep", "999"], timeout=0.2)
    name = next(a for a in fake.run_argv if a.startswith("--name="))[len("--name=") :]
    assert fake.rm_calls == [["docker", "rm", "--force", name]]
    # The CLI is killed (and reaped) first, so it cannot create or start the
    # container after the removal.
    assert fake.events.index("kill") < fake.events.index("rm")


async def test_removal_is_retried_once_when_it_fails(fake_docker, repo):
    fake = fake_docker(run_proc=FakeProc(hang=True), rm_codes=(1, 0))
    with pytest.raises(CommandTimeoutError):
        await DockerSandbox(repo, CONFIG).exec_command(["sleep", "9"], timeout=0.2)
    assert len(fake.rm_calls) == 2
    assert fake.events == ["stdin-closed", "kill", "rm", "rm"]


async def test_removal_gives_up_after_one_retry(fake_docker, repo):
    fake = fake_docker(run_proc=FakeProc(hang=True), rm_codes=(1,))
    with pytest.raises(CommandTimeoutError):
        await DockerSandbox(repo, CONFIG).exec_command(["sleep", "9"], timeout=0.2)
    assert len(fake.rm_calls) == 2


async def test_cancellation_removes_container(fake_docker, repo):
    fake = fake_docker(run_proc=FakeProc(hang=True))
    task = asyncio.ensure_future(
        DockerSandbox(repo, CONFIG).exec_command(["sleep", "999"], timeout=60)
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(fake.rm_calls) == 1
    assert fake.events.index("kill") < fake.events.index("rm")


async def test_stream_error_removes_container(fake_docker, repo):
    fake = fake_docker(run_proc=FakeProc([b"x"], error=OSError("pipe broke")))
    with pytest.raises(OSError):
        await DockerSandbox(repo, CONFIG).exec_command(["true"])
    assert len(fake.rm_calls) == 1


async def test_packing_time_counts_against_the_timeout(fake_docker, monkeypatch, repo):
    def slow_archive(src):
        import time

        time.sleep(0.5)
        return io.BytesIO(b"")

    monkeypatch.setattr(docker_sandbox, "_archive_workspace", slow_archive)
    fake = fake_docker()
    with pytest.raises(CommandTimeoutError, match="pack"):
        await DockerSandbox(repo, CONFIG).exec_command(["true"], timeout=0.1)
    # No container was started for a copy that never finished.
    assert fake.calls == []


async def test_packing_failure_is_a_runner_error(fake_docker, monkeypatch, repo):
    def broken(src):
        raise PermissionError("denied")

    monkeypatch.setattr(docker_sandbox, "_archive_workspace", broken)
    fake = fake_docker()
    with pytest.raises(RunnerError, match="pack the working copy"):
        await DockerSandbox(repo, CONFIG).exec_command(["true"])
    assert fake.calls == []


async def test_container_exiting_before_reading_stdin_is_not_a_crash(fake_docker, repo):
    fake_docker(
        run_proc=FakeProc([b"Unable to find image"], returncode=125, broken_stdin=True)
    )
    with pytest.raises(RunnerError, match="Unable to find image"):
        await DockerSandbox(repo, CONFIG).exec_command(["true"])


async def test_missing_docker_cli_is_a_runner_error(fake_docker, repo):
    fake_docker(start_error=FileNotFoundError("docker"))
    with pytest.raises(RunnerError, match="docker CLI"):
        await DockerSandbox(repo, CONFIG).exec_command(["true"])


async def test_docker_exit_125_is_a_runner_error_not_a_test_failure(fake_docker, repo):
    fake_docker(run_proc=FakeProc([b"Unable to find image"], returncode=125))
    with pytest.raises(RunnerError, match="Unable to find image"):
        await DockerSandbox(repo, CONFIG).exec_command(["true"])


async def test_test_failure_exit_code_is_returned(fake_docker, repo):
    fake_docker(run_proc=FakeProc([b"1 failed\n"], returncode=1))
    code, out, _ = await DockerSandbox(repo, CONFIG).exec_command(["pytest"])
    assert (code, out) == (1, "1 failed\n")


async def test_output_is_capped_to_the_tail(fake_docker, repo):
    config = DockerSandboxConfig(max_output_bytes=1024)
    chunks = [b"a" * 1000, b"b" * 1000, b"c" * 1000 + b"END"]
    fake_docker(run_proc=FakeProc(chunks, returncode=1))
    code, out, note = await DockerSandbox(repo, config).exec_command(["x"])
    assert code == 1
    assert len(out.encode()) == 1024
    assert out.endswith("END")
    assert "truncated" in note and "3003" in note


def test_max_output_follows_verify_max_output_bytes(monkeypatch):
    monkeypatch.setenv("VERIFY_MAX_OUTPUT_BYTES", "4096")
    assert DockerSandboxConfig.from_env().max_output_bytes == 4096
    monkeypatch.setenv("VERIFY_MAX_OUTPUT_BYTES", "10")
    assert DockerSandboxConfig.from_env().max_output_bytes == 1024


async def test_verify_working_copy_through_docker_backend(
    fake_docker, monkeypatch, repo
):
    monkeypatch.setenv("SANDBOX_BACKEND", "runners.docker_sandbox:create")
    monkeypatch.setenv("VERIFY_MAX_OUTPUT_BYTES", "1024")
    fake = fake_docker(run_proc=FakeProc([b"z" * 5000 + b"\n1 passed\n"]))
    result = await verification.verify_working_copy(repo)
    assert result.status == "passed"
    assert result.runner == "sandbox"
    assert result.truncated is True
    assert len(result.output_tail.encode()) <= 1024
    assert "truncated" in result.output_tail
    assert fake.run_argv[-4:] == ["python", "-m", "pytest", "-q"]


async def test_verify_working_copy_reports_timeout_as_error(
    fake_docker, monkeypatch, repo
):
    (repo / ".automaintainer.yml").write_text(
        'test_command: ["sleep", "99"]\ntimeout_seconds: 1\n', encoding="utf-8"
    )
    fake = fake_docker(run_proc=FakeProc(hang=True))
    result = await verification.verify_working_copy(
        repo, sandbox_factory=lambda p: DockerSandbox(p, CONFIG)
    )
    assert result.status == "error"
    assert "timed out" in result.summary
    assert len(fake.rm_calls) == 1


# --- file operations, diff, push ----------------------------------------------


async def test_file_operations_are_confined_to_the_working_copy(repo):
    sandbox = DockerSandbox(repo, CONFIG)
    await sandbox.write_file("pkg/new.py", "Y = 2\n")
    assert await sandbox.read_file("pkg/new.py") == "Y = 2\n"
    assert "pkg/new.py" in await sandbox.list_files("*.py")
    await sandbox.delete_file("pkg/new.py")
    assert not (repo / "pkg" / "new.py").exists()
    for bad in ("../outside.txt", "pkg/../../outside.txt"):
        with pytest.raises(PathTraversalError):
            await sandbox.write_file(bad, "x")
        with pytest.raises(PathTraversalError):
            await sandbox.read_file(bad)
        with pytest.raises(PathTraversalError):
            await sandbox.delete_file(bad)
    assert not (repo.parent / "outside.txt").exists()


async def test_commit_and_push_is_not_supported(repo):
    with pytest.raises(NotImplementedError, match="never commits or pushes"):
        await DockerSandbox(repo, CONFIG).commit_and_push("b", "m", dry_run=True)


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
    diff = await DockerSandbox(repo, CONFIG).get_git_diff()
    assert "-X = 1" in diff and "+X = 2" in diff
