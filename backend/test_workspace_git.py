"""Workspace naming, server-side git credential handling, and the Implementer's
relationship with the shared local clone."""

import asyncio
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

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


# --- workspace keyed by GitHub repository id (#392) ------------------------------


def test_renamed_or_transferred_repo_keeps_one_clone():
    before = get_safe_repo_dir("acme/app", github_repo_id=42)
    assert get_safe_repo_dir("acme/app-renamed", github_repo_id=42) == before
    assert get_safe_repo_dir("other-owner/app", github_repo_id=42) == before


def test_recreated_repo_with_same_name_gets_a_fresh_clone():
    """A deleted and recreated repository has a new id; it must not inherit
    the old repository's working tree."""
    old = get_safe_repo_dir("acme/app", github_repo_id=42)
    assert get_safe_repo_dir("acme/app", github_repo_id=43) != old


def test_id_keyed_dir_is_inside_the_workspace_and_disjoint_from_name_keys():
    base = workspace.get_base_workspace_dir()
    by_id = get_safe_repo_dir("acme/app", github_repo_id=42)
    assert by_id == base / workspace.REPO_ID_DIR / "42"
    assert by_id.parent.is_dir()
    # No repository name maps onto the id directory or into it.
    for name in ("_by_id", "_by_id/42", "acme/app"):
        try:
            assert not get_safe_repo_dir(name).is_relative_to(by_id.parent)
        except HTTPException:
            pass


def test_without_an_id_the_name_hash_is_kept():
    assert get_safe_repo_dir("acme/app", github_repo_id=None) == get_safe_repo_dir(
        "acme/app"
    )


@pytest.mark.parametrize("bad", [0, -1, True, "42", "../x", 4.2])
def test_invalid_repo_id_is_rejected(bad):
    with pytest.raises(HTTPException):
        get_safe_repo_dir("acme/app", github_repo_id=bad)


def test_id_key_still_validates_the_name():
    with pytest.raises(HTTPException):
        get_safe_repo_dir("../etc", github_repo_id=42)


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
def test_sync_follows_a_renamed_repository_into_its_existing_clone(upstream, tmp_path):
    """A clone keyed by repository id outlives a rename; updating it must use
    the repository's current URL, not the clone's stale origin."""
    repo_dir = str(get_safe_repo_dir("o/old", github_repo_id=7))
    assert asyncio.run(
        sync_repo_clone("o/old", repo_dir, None, remote_url=str(upstream))
    )

    moved = tmp_path / "renamed"
    upstream.rename(moved)
    (moved / "app.py").write_text("v2\n")
    git("commit", "-qam", "c2", cwd=moved)

    assert str(get_safe_repo_dir("o/new", github_repo_id=7)) == repo_dir
    assert asyncio.run(sync_repo_clone("o/new", repo_dir, None, remote_url=str(moved)))
    assert open(os.path.join(repo_dir, "app.py")).read() == "v2\n"


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


# --- Implementer and the shared clone --------------------------------------------


def _implementer_state(idea):
    return {
        "repo_name": "o/r",
        "idea": idea,
        "issue_number": 7,
        "iteration": 0,
        "review": "",
        "branch_name": "",
        "target_file_path": "",
        "pr_number": 0,
    }


def _link_dir(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("cannot create a directory symlink here")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def test_implementer_does_not_write_through_repo_symlinks(tmp_path, monkeypatch):
    """A symlinked directory committed to the target repo redirected the
    Implementer's local write outside the workspace."""
    import agents
    from github import GithubException

    repo_dir = get_safe_repo_dir("o/r")
    repo_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _link_dir(outside, repo_dir / "docs")

    gh_repo = MagicMock()
    gh_repo.default_branch = "main"
    gh_repo.get_contents.side_effect = GithubException(404, {}, None)
    gh_repo.create_pull.return_value = SimpleNamespace(number=1, html_url="u")
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: gh_repo))

    async def fake_llm(*a, **k):
        return "PAYLOAD"

    monkeypatch.setattr(agents, "run_llm_with_tools", fake_llm)

    asyncio.run(agents.implementer_node(_implementer_state("Fix docs/evil.py:1-2")))

    assert not (outside / "evil.py").exists()
    gh_repo.create_file.assert_called_once()


@requires_git
def test_implementer_leaves_default_branch_clone_clean(upstream, monkeypatch):
    """PR code written into the shared default-branch clone made the next
    `git pull --ff-only` fail, so the clone went permanently stale."""
    import agents

    repo_dir = get_safe_repo_dir("o/r")
    assert (
        git("clone", "-q", str(upstream), str(repo_dir), cwd=upstream).returncode == 0
    )

    gh_repo = MagicMock()
    gh_repo.default_branch = "main"
    existing = MagicMock()
    existing.decoded_content = b"v1\n"
    gh_repo.get_contents.return_value = existing
    gh_repo.create_pull.return_value = SimpleNamespace(number=1, html_url="u")
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=lambda _: gh_repo))

    async def fake_llm(*a, **k):
        return "unmerged-change\n"

    monkeypatch.setattr(agents, "run_llm_with_tools", fake_llm)

    asyncio.run(agents.implementer_node(_implementer_state("Fix app.py:1-1")))

    assert git("status", "--porcelain", cwd=repo_dir).stdout == ""
    (upstream / "app.py").write_text("v2\n")
    git("commit", "-qam", "c2", cwd=upstream)
    assert git("pull", "--ff-only", cwd=repo_dir).returncode == 0
