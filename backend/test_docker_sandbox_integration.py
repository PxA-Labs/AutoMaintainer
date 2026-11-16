"""Integration tests for runners/docker_sandbox.py against a real Docker
daemon, through verification.verify_working_copy.

Skipped unless Docker can run Linux containers here AND
``DOCKER_SANDBOX_TESTS=1`` is set. The opt-in keeps the general backend CI
(which also runs on Windows runners, whose Docker cannot run the Linux image)
and developer machines from pulling images; .github/workflows/sandbox.yml
sets it on ubuntu-latest, and runs this file twice: against the runner's
daemon and against a Docker-in-Docker daemon reached through ``DOCKER_HOST``
that cannot see the worker's filesystem.
"""

import os
import shutil
import subprocess
import textwrap
import uuid

import pytest

import verification
from runners.docker_sandbox import CONTAINER_PREFIX, DockerSandboxConfig


def _docker_usable() -> bool:
    if os.getenv("DOCKER_SANDBOX_TESTS") != "1" or shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.strip() == "linux"


pytestmark = pytest.mark.skipif(
    not _docker_usable(),
    reason="needs a Linux Docker daemon and DOCKER_SANDBOX_TESTS=1",
)

CANARY = "AUTOMAINTAINER_SANDBOX_CANARY"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("VERIFY_CHANGES", "VERIFY_ALLOW_LOCAL", "SANDBOX_NETWORK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SANDBOX_BACKEND", "runners.docker_sandbox:create")
    # A host secret the sandboxed tests must not see.
    monkeypatch.setenv(CANARY, "leaked-" + uuid.uuid4().hex)


def _repo(tmp_path, test_body: str, timeout: int = 120):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / ".automaintainer.yml").write_text(
        'test_command: ["python", "-m", "unittest", "discover", "-v", "-s", "tests"]\n'
        f"timeout_seconds: {timeout}\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_it.py").write_text(
        "import os, socket, unittest\n\n\nclass T(unittest.TestCase):\n"
        + textwrap.indent(textwrap.dedent(test_body), "    "),
        encoding="utf-8",
    )
    return root


def _leftover_containers():
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={CONTAINER_PREFIX}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


async def test_passing_repository_passes(tmp_path):
    repo = _repo(tmp_path, "def test_ok(self):\n    self.assertEqual(1 + 1, 2)\n")
    result = await verification.verify_working_copy(repo)
    assert result.status == "passed", result.output_tail
    assert result.runner == "sandbox"
    assert _leftover_containers() == []


async def test_failing_repository_fails(tmp_path):
    repo = _repo(tmp_path, "def test_bad(self):\n    self.assertEqual(1, 2)\n")
    result = await verification.verify_working_copy(repo)
    assert result.status == "failed", result.output_tail
    assert result.exit_code == 1
    assert _leftover_containers() == []


async def test_timeout_is_an_error_and_the_container_is_removed(tmp_path):
    repo = _repo(
        tmp_path,
        "def test_hang(self):\n    import time\n    time.sleep(600)\n",
        timeout=5,
    )
    result = await verification.verify_working_copy(repo)
    assert result.status == "error"
    assert "timed out" in result.summary
    assert _leftover_containers() == []


async def test_network_and_host_environment_are_unavailable(tmp_path):
    repo = _repo(
        tmp_path,
        f"""
        def test_no_network(self):
            with self.assertRaises(OSError):
                socket.create_connection(("1.1.1.1", 53), timeout=5)

        def test_no_dns(self):
            with self.assertRaises(OSError):
                socket.getaddrinfo("github.com", 443)

        def test_no_host_env(self):
            self.assertNotIn("{CANARY}", os.environ)
            self.assertNotIn("GITHUB_TOKEN", os.environ)

        def test_not_root_and_root_fs_read_only(self):
            self.assertNotEqual(os.getuid(), 0)
            with self.assertRaises(OSError):
                open("/etc/automaintainer-probe", "w")
            with open("/tmp/probe", "w") as f:
                f.write("tmp is writable")
        """,
    )
    result = await verification.verify_working_copy(repo)
    assert result.status == "passed", result.output_tail
    assert "Ran 4 tests" in result.output_tail


async def test_writes_do_not_reach_the_host_clone(tmp_path):
    repo = _repo(
        tmp_path,
        """
        def test_tamper(self):
            with open("planted.txt", "w") as f:
                f.write("x")
            with open("tests/test_it.py", "a") as f:
                f.write("\\nraise SystemExit('tampered')\\n")
        """,
    )
    original = (repo / "tests" / "test_it.py").read_text(encoding="utf-8")
    result = await verification.verify_working_copy(repo)
    assert result.status == "passed", result.output_tail
    assert not (repo / "planted.txt").exists()
    assert (repo / "tests" / "test_it.py").read_text(encoding="utf-8") == original


async def test_workspace_is_a_complete_writable_copy(tmp_path):
    repo = _repo(
        tmp_path,
        """
        def test_copy(self):
            self.assertEqual(os.getcwd(), "/workspace")
            with open("data/nested/value.txt") as f:
                self.assertEqual(f.read(), "42")
            with open("data/nested/new.txt", "w") as f:
                f.write("tests may write to their copy")
        """,
    )
    (repo / "data" / "nested").mkdir(parents=True)
    (repo / "data" / "nested" / "value.txt").write_text("42", encoding="utf-8")
    result = await verification.verify_working_copy(repo)
    assert result.status == "passed", result.output_tail
    assert not (repo / "data" / "nested" / "new.txt").exists()


def test_config_defaults_used_in_ci_are_hardened():
    config = DockerSandboxConfig.from_env()
    assert config.network == "none"
    assert config.user.split(":")[0] != "0"
