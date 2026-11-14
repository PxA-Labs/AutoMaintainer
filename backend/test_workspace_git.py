"""Workspace naming, server-side git credential handling, and the Implementer's
relationship with the shared local clone."""

import asyncio
import os
import shutil
import subprocess

import pytest
from fastapi import HTTPException

import workspace
from workspace import get_safe_repo_dir, sync_repo_clone

TOKEN = "ghs_FAKE_TOKEN_123"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path / "ws"))
    # The legacy-clone scrub runs once per process; re-arm it for every test.
    monkeypatch.setattr(workspace, "_workspace_scrubbed", False)
    yield


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True
    )


@pytest.fixture
def upstream(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    git("init", "-q", "-b", "main", cwd=up)
    (up / "app.py").write_text("v1\n")
    git("add", ".", cwd=up)
    git("commit", "-qm", "c1", cwd=up)
    return up


# --- get_safe_repo_dir ---------------------------------------------------------


@pytest.mark.parametrize("name", ["owner/repo\n", "a/b\n", "repo\n", "owner/repo\r\n"])
def test_repo_name_rejects_trailing_newline(name):
    """re.match with "$" accepted a trailing newline and mapped it elsewhere."""
    with pytest.raises(HTTPException):
        get_safe_repo_dir(name)


@pytest.mark.parametrize(
    "first,second", [("a/b_c", "a_b/c"), ("a/b", "a_b"), ("x/y.z", "x.y/z")]
)
def test_distinct_repos_get_distinct_dirs(first, second):
    """Flattening "/" to "_" alone made two repositories share one clone."""
    assert get_safe_repo_dir(first) != get_safe_repo_dir(second)


def test_repo_dir_is_stable_and_case_insensitive():
    """GitHub names are case-insensitive: one repository, one clone."""
    assert get_safe_repo_dir("Owner/Repo") == get_safe_repo_dir("owner/repo")
    assert get_safe_repo_dir("owner/repo") == get_safe_repo_dir("owner/repo")


@pytest.mark.parametrize("name", ["../x", "a/..", "..", ".", "a/b/c", "", "a b/c"])
def test_repo_name_rejects_invalid(name):
    with pytest.raises(HTTPException):
        get_safe_repo_dir(name)


# --- git credentials -----------------------------------------------------------


def test_git_auth_env_carries_token_outside_argv():
    env = workspace.git_auth_env(TOKEN)

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in workspace.git_auth_env(None)


def test_sync_never_puts_token_in_argv(monkeypatch, tmp_path):
    """`-c http.extraheader=...` exposed the token in argv (ps) and, on clone,
    persisted it in .git/config."""
    calls = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        calls.append((argv, kwargs.get("env") or {}))
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    repo_dir = str(tmp_path / "clone")
    assert asyncio.run(sync_repo_clone("o/r", repo_dir, TOKEN))
    os.makedirs(repo_dir)
    assert asyncio.run(sync_repo_clone("o/r", repo_dir, TOKEN))

    subcommands = [argv[1] for argv, _ in calls]
    assert subcommands == ["clone", "fetch", "reset"]
    for argv, env in calls:
        assert not any(TOKEN in a or "extraheader" in a for a in argv), argv
        assert "-c" not in argv
    assert calls[0][0][-2:] == ("https://github.com/o/r.git", repo_dir)


@requires_git
def test_clone_does_not_persist_token(upstream, tmp_path):
    repo_dir = str(get_safe_repo_dir("o/r"))

    assert asyncio.run(
        sync_repo_clone("o/r", repo_dir, TOKEN, remote_url=str(upstream))
    )

    config = open(os.path.join(repo_dir, ".git", "config")).read()
    assert "extraheader" not in config.lower()
    assert "AUTHORIZATION" not in config


@requires_git
def test_sync_scrubs_token_persisted_by_old_clones(upstream, tmp_path):
    """Clones made with `clone -c http.extraheader=...` kept the token on disk,
    including clones under the previous (unhashed) directory naming."""
    repo_dir = str(get_safe_repo_dir("o/r"))
    legacy_dir = str(workspace.get_base_workspace_dir() / "o_r")
    for d in (repo_dir, legacy_dir):
        result = git(
            "clone",
            "-q",
            "-c",
            "http.extraheader=AUTHORIZATION: basic FAKE",
            str(upstream),
            d,
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert "FAKE" in open(os.path.join(d, ".git", "config")).read()

    assert asyncio.run(
        sync_repo_clone("o/r", repo_dir, TOKEN, remote_url=str(upstream))
    )

    for d in (repo_dir, legacy_dir):
        assert "FAKE" not in open(os.path.join(d, ".git", "config")).read(), d


@requires_git
def test_sync_recovers_dirty_clone_and_keeps_untracked(upstream):
    """A dirty tree made `pull --ff-only` fail on every run, silently."""
    repo_dir = str(get_safe_repo_dir("o/r"))
    assert asyncio.run(sync_repo_clone("o/r", repo_dir, None, remote_url=str(upstream)))

    with open(os.path.join(repo_dir, "app.py"), "w") as f:
        f.write("unmerged local edit\n")
    os.makedirs(os.path.join(repo_dir, ".gitnexus"))
    (upstream / "app.py").write_text("v2\n")
    git("commit", "-qam", "c2", cwd=upstream)

    assert asyncio.run(sync_repo_clone("o/r", repo_dir, None, remote_url=str(upstream)))

    assert open(os.path.join(repo_dir, "app.py")).read() == "v2\n"
    assert os.path.isdir(os.path.join(repo_dir, ".gitnexus"))


@requires_git
def test_failed_clone_reports_failure_and_leaves_no_directory(tmp_path):
    repo_dir = str(get_safe_repo_dir("o/r"))

    ok = asyncio.run(
        sync_repo_clone("o/r", repo_dir, None, remote_url=str(tmp_path / "missing"))
    )

    assert ok is False
    assert not os.path.exists(repo_dir)


def test_git_timeout_kills_process(monkeypatch, tmp_path):
    killed = []

    class SlowProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            killed.append(True)

        async def wait(self):
            return -9

    async def fake_exec(*argv, **kwargs):
        return SlowProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    repo_dir = tmp_path / "clone"
    repo_dir.mkdir()

    ok = asyncio.run(sync_repo_clone("o/r", str(repo_dir), None, timeout=0.05))

    assert ok is False
    assert killed
