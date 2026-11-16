"""Each IDE user reads their own working branch, never the shared clone (#505).

The IDE used to read one clone per repository while its writes went to a
per-user working branch, so a file one user created or saved showed up for
every other user of the repository (and in their Propose), and the staged
list was lost on reload. These tests run real git: "GitHub" is a local bare
repository that the faked PyGithub calls commit to, and the shared clone is a
real clone of it. Nothing here calls GitHub or Supabase.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from github import UnknownObjectException

import agents
import main
import workspace
from main import app

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

client = TestClient(app)

ORG = "org-1"
ALICE, BOB = "alice", "bob"
REPO = "o/r"


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


class FakeGitHub:
    """The PyGithub calls the IDE routes make, applied to a bare repository."""

    default_branch = "main"

    def __init__(self, origin: Path, scratch: Path):
        self.origin = origin
        self.scratch = scratch
        self.proposed = []
        git(scratch.parent, "clone", "-q", str(origin), str(scratch))

    def sha(self, branch):
        out = git(
            self.origin,
            "for-each-ref",
            "--format=%(objectname)",
            f"refs/heads/{branch}",
        )
        return out.strip() or None

    def commit(self, branch, files, message="edit"):
        """Commit ``files`` ({path: content, or None to delete}) to ``branch``."""
        s = self.scratch
        git(s, "fetch", "-q", "origin")
        git(s, "checkout", "-q", "-B", branch, f"origin/{branch}")
        for path, content in files.items():
            target = s / path
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        git(s, "add", "-A")
        git(s, "commit", "-q", "-m", message)
        git(s, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    # --- PyGithub surface ---------------------------------------------------

    def get_branch(self, branch):
        if not self.sha(branch):
            raise UnknownObjectException(404, {"message": "Branch not found"}, None)
        return SimpleNamespace(protected=False)

    def get_git_ref(self, ref):
        branch = ref.split("/", 1)[1]
        return SimpleNamespace(
            object=SimpleNamespace(sha=self.sha(branch)),
            delete=lambda: git(self.origin, "update-ref", "-d", f"refs/{ref}"),
        )

    def create_git_ref(self, ref, sha):
        git(self.origin, "update-ref", ref, sha)

    def merge(self, base, head, commit_message):
        return None  # already up to date

    def get_contents(self, path, ref):
        try:
            git(self.origin, "cat-file", "-e", f"refs/heads/{ref}:{path}")
        except subprocess.CalledProcessError:
            raise UnknownObjectException(404, {"message": "Not Found"}, None)
        return SimpleNamespace(path=path, sha="blob")

    def update_file(self, path, message, content, sha, branch):
        self.proposed.append((branch, path, content))
        self.commit(branch, {path: content})

    def create_file(self, path, message, content, branch):
        self.proposed.append((branch, path, content))
        self.commit(branch, {path: content})

    def delete_file(self, path, message, sha, branch):
        self.proposed.append((branch, path, None))
        self.commit(branch, {path: None})

    def create_pull(self, title, body, head, base):
        return SimpleNamespace(number=7, html_url="https://example.invalid/pr/7")


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setattr(workspace, "_no_branch_seen", {})
    monkeypatch.setattr(workspace, "_clone_locks", {})

    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "a.py").write_text("original\n", encoding="utf-8")
    (seed / "README.md").write_text("# readme\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "main")

    clone = workspace.get_safe_repo_dir(REPO)
    git(tmp_path, "clone", "-q", str(origin), str(clone))

    gh = FakeGitHub(origin, tmp_path / "scratch")
    monkeypatch.setattr(workspace, "github_remote_url", lambda _name: str(origin))
    monkeypatch.setattr(
        main, "Github", lambda *_: SimpleNamespace(get_repo=lambda _: gh)
    )

    async def own_repo(*_a, **_k):
        return None, None

    monkeypatch.setattr(agents, "supabase", MagicMock())
    # propose-changes checks the tenant itself; the tenant checks have their
    # own tests (test_route_inventory.py).
    monkeypatch.setattr(main, "validate_repo_access", own_repo)
    app.dependency_overrides[main.require_repo_access] = lambda: ORG
    yield SimpleNamespace(origin=origin, clone=clone, gh=gh, tmp=tmp_path)
    app.dependency_overrides.clear()


def as_user(user_id):
    async def user():
        return {"user_id": user_id, "org_id": ORG, "role": "member", "email": "e"}

    app.dependency_overrides[main.get_current_user] = user


def read(user_id, path):
    as_user(user_id)
    return client.get(f"/repo/{REPO}/file", params={"file_path": path})


def tree_paths(user_id) -> set:
    as_user(user_id)
    r = client.get(f"/repo/{REPO}/tree")
    assert r.status_code == 200, r.text
    paths = set()

    def walk(nodes):
        for node in nodes:
            paths.add(node["path"])
            walk(node.get("children", []))

    walk(r.json()["children"])
    return paths


def changes(user_id) -> dict:
    as_user(user_id)
    r = client.get(f"/repo/{REPO}/working-changes")
    assert r.status_code == 200, r.text
    return r.json()


def save(user_id, path, content):
    as_user(user_id)
    r = client.post(f"/repo/{REPO}/file", json={"file_path": path, "content": content})
    assert r.status_code == 200, r.text
    return r.json()


def create(user_id, path, content=""):
    as_user(user_id)
    r = client.post(
        f"/repo/{REPO}/file/create", json={"file_path": path, "content": content}
    )
    assert r.status_code == 200, r.text
    return r.json()


def delete(user_id, path):
    as_user(user_id)
    r = client.delete(f"/repo/{REPO}/file", params={"file_path": path})
    assert r.status_code == 200, r.text


# --- isolation -----------------------------------------------------------------


def test_saved_edit_is_seen_by_its_author_only(world):
    assert read(BOB, "a.py").json() == {"content": "original\n"}

    save(ALICE, "a.py", "alice's edit\n")

    assert read(ALICE, "a.py").json() == {"content": "alice's edit\n"}
    assert read(BOB, "a.py").json() == {"content": "original\n"}
    # The shared clone is never written.
    assert (world.clone / "a.py").read_text(encoding="utf-8") == "original\n"
    # The default branch is never written either.
    assert world.gh.sha("main") == git(world.clone, "rev-parse", "HEAD").strip()


def test_created_file_is_visible_to_its_creator_only(world):
    create(ALICE, "src/new.py", "secret = 1\n")

    assert "src/new.py" in tree_paths(ALICE)
    assert "src/new.py" not in tree_paths(BOB)
    assert read(BOB, "src/new.py").status_code == 404
    as_user(BOB)
    assert (
        client.get(f"/repo/{REPO}/search", params={"q": "secret"}).json()["results"]
        == []
    )
    as_user(ALICE)
    hits = client.get(f"/repo/{REPO}/search", params={"q": "secret"}).json()
    assert [h["file"] for h in hits["results"]] == ["src/new.py"]
    # No untracked file in the shared clone.
    assert not (world.clone / "src" / "new.py").exists()


def test_untracked_files_in_the_shared_clone_are_never_served(world):
    """Leftovers in the shared clone (an agent run, or IDE creates mirrored by
    older releases) are not part of any user's view."""
    (world.clone / "leak.txt").write_text("leak\n", encoding="utf-8")
    save(ALICE, "a.py", "edit\n")

    for user in (ALICE, BOB):
        assert "leak.txt" not in tree_paths(user)
        assert read(user, "leak.txt").status_code == 404


def test_deleted_file_disappears_for_its_author_only(world):
    delete(ALICE, "README.md")

    assert "README.md" not in tree_paths(ALICE)
    assert "README.md" in tree_paths(BOB)


# --- staged-list restore and Propose ---------------------------------------------


def test_working_changes_is_the_callers_branch_diff(world):
    save(ALICE, "a.py", "alice\n")
    create(ALICE, "new.py", "n\n")
    delete(ALICE, "README.md")
    # The default branch moves on after Alice's branch was created: those
    # commits are not Alice's changes.
    world.gh.commit("main", {"upstream.py": "u\n"})
    save(BOB, "a.py", "bob\n")

    alice = changes(ALICE)
    assert alice["branch"] == main._ide_working_branch(ORG, ALICE)
    assert sorted(alice["changes"], key=lambda c: c["path"]) == [
        {"path": "README.md", "status": "deleted"},
        {"path": "a.py", "status": "modified"},
        {"path": "new.py", "status": "created"},
    ]
    assert changes(BOB)["changes"] == [{"path": "a.py", "status": "modified"}]


def test_change_that_reached_the_default_branch_is_no_longer_listed(world):
    """Once the proposed pull request merges, the file is not staged again."""
    save(ALICE, "a.py", "alice\n")
    create(ALICE, "new.py", "n\n")
    assert len(changes(ALICE)["changes"]) == 2

    world.gh.commit("main", {"a.py": "alice\n"}, message="merge the PR")

    assert changes(ALICE)["changes"] == [{"path": "new.py", "status": "created"}]


def test_user_without_a_working_branch_has_no_changes(world):
    save(ALICE, "a.py", "alice\n")

    assert changes(BOB) == {"branch": None, "changes": []}


def test_restored_staged_list_proposes_only_the_callers_changes(world):
    """What the dashboard does after a reload: restore the staged list from
    working-changes, load each file from the caller's view, propose it."""
    save(ALICE, "a.py", "alice\n")
    create(ALICE, "new.py", "n\n")
    save(BOB, "a.py", "bob\n")
    create(BOB, "bob_only.py", "b\n")

    staged = changes(ALICE)["changes"]
    payload = [
        {
            "path": c["path"],
            "status": c["status"],
            "content": read(ALICE, c["path"]).json()["content"],
        }
        for c in staged
    ]
    world.gh.proposed.clear()
    as_user(ALICE)
    r = client.post(
        f"/repo/{REPO}/propose-changes", json={"title": "t", "changes": payload}
    )

    assert r.status_code == 200, r.text
    branch = r.json()["branch_name"]
    assert sorted(
        (path, content) for b, path, content in world.gh.proposed if b == branch
    ) == [("a.py", "alice\n"), ("new.py", "n\n")]


@pytest.mark.parametrize(
    "method,route,body",
    [
        ("post", "file", {"file_path": "a.py", "content": "bob\n"}),
        ("post", "file/create", {"file_path": "evil.py", "content": "bob\n"}),
        ("delete", "file", None),
    ],
)
@pytest.mark.parametrize("upper", [False, True])
def test_users_cannot_write_into_another_users_working_branch(
    world, method, route, body, upper
):
    """The working-branch names are derivable, so naming Alice's branch
    explicitly must not let Bob put code into Alice's Propose."""
    save(ALICE, "a.py", "alice\n")
    alice_branch = main._ide_working_branch(ORG, ALICE)
    target = alice_branch.upper() if upper else alice_branch
    before = world.gh.sha(alice_branch)
    world.gh.proposed.clear()

    as_user(BOB)
    if method == "post":
        r = client.post(f"/repo/{REPO}/{route}", json={**body, "branch": target})
    else:
        r = client.delete(
            f"/repo/{REPO}/{route}",
            params={"file_path": "README.md", "branch": target},
        )

    assert r.status_code == 403, r.text
    assert world.gh.proposed == []
    assert world.gh.sha(alice_branch) == before
    assert changes(ALICE)["changes"] == [{"path": "a.py", "status": "modified"}]
    assert read(ALICE, "a.py").json() == {"content": "alice\n"}


def test_naming_ones_own_working_branch_explicitly_still_works(world):
    as_user(ALICE)
    r = client.post(
        f"/repo/{REPO}/file",
        json={
            "file_path": "a.py",
            "content": "alice\n",
            "branch": main._ide_working_branch(ORG, ALICE),
        },
    )

    assert r.status_code == 200, r.text
    assert read(ALICE, "a.py").json() == {"content": "alice\n"}


# --- view lifecycle ----------------------------------------------------------------


def test_merged_and_deleted_branch_falls_back_to_the_default_branch(world):
    save(ALICE, "a.py", "alice\n")
    branch = main._ide_working_branch(ORG, ALICE)
    view = workspace.ide_view_dir(world.clone, workspace.ide_user_key(ORG, ALICE))
    assert view.is_dir()

    git(world.origin, "update-ref", "-d", f"refs/heads/{branch}")

    assert "a.py" in tree_paths(ALICE)
    assert read(ALICE, "a.py").json() == {"content": "original\n"}
    assert not view.exists()
    assert changes(ALICE) == {"branch": None, "changes": []}


def test_file_read_after_the_view_was_pruned_asks_github_again(world):
    """A pruned view must not be replaced by the default branch silently."""
    save(ALICE, "a.py", "alice\n")
    workspace.prune_ide_views(force=True, now=time.time() + 10 * 86400)

    assert read(ALICE, "a.py").json() == {"content": "alice\n"}


def test_unreachable_github_serves_the_last_fetched_view(world, monkeypatch):
    save(ALICE, "a.py", "alice\n")
    monkeypatch.setattr(
        workspace, "github_remote_url", lambda _n: str(world.tmp / "missing.git")
    )

    assert "a.py" in tree_paths(ALICE)
    assert read(ALICE, "a.py").json() == {"content": "alice\n"}
    assert read(BOB, "a.py").json() == {"content": "original\n"}


def test_views_expire_and_are_capped(world, monkeypatch):
    save(ALICE, "a.py", "alice\n")
    save(BOB, "a.py", "bob\n")
    alice_key = workspace.ide_user_key(ORG, ALICE)
    bob_key = workspace.ide_user_key(ORG, BOB)
    alice_view = workspace.ide_view_dir(world.clone, alice_key)
    bob_view = workspace.ide_view_dir(world.clone, bob_key)
    old = time.time() - 3 * 86400
    os.utime(bob_view, (old, old))

    removed = workspace.prune_ide_views(force=True)

    assert removed == [bob_view]
    assert alice_view.is_dir() and not bob_view.exists()
    # The worktree and its pinned refs are gone from the shared clone too.
    assert str(bob_view.name) not in git(world.clone, "worktree", "list")
    assert bob_key not in git(world.clone, "for-each-ref")

    # Over the cap, the least recently used view goes first; the one being
    # served is always kept.
    read(BOB, "a.py")  # recreates Bob's view
    os.utime(alice_view, (old, old))
    monkeypatch.setenv("IDE_VIEW_MAX", "1")
    assert workspace.prune_ide_views(force=True) == [alice_view]
    assert workspace.prune_ide_views(keep={bob_view}, force=True) == []


# --- path safety ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path,status",
    [
        ("../a.py", 400),
        ("src/../../a.py", 400),
        (".git", 403),
        (".git/config", 403),
        (".GIT/HEAD", 403),
    ],
)
def test_view_reads_refuse_traversal_and_git_metadata(world, path, status):
    save(ALICE, "a.py", "alice\n")

    assert read(ALICE, path).status_code == status


def test_view_git_pointer_is_not_listed_or_searched(world):
    """A view's .git is a file naming a server path; it is never served."""
    save(ALICE, "a.py", "alice\n")

    assert ".git" not in tree_paths(ALICE)
    as_user(ALICE)
    r = client.get(f"/repo/{REPO}/search", params={"q": "gitdir"})
    assert r.json()["results"] == []


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlinks")
def test_committed_symlinks_do_not_escape_the_view(world):
    """A symlink on the user's branch pointing at another user's view or
    outside the workspace is not followed by file, tree or search."""
    create(BOB, "b.py", "bob secret\n")
    outside = world.tmp / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    bob_key = workspace.ide_user_key(ORG, BOB)
    s = world.gh.scratch
    git(s, "fetch", "-q", "origin")
    git(s, "checkout", "-q", "-B", "tmp", "origin/main")
    os.symlink(f"../{bob_key}/b.py", s / "to_bob")
    os.symlink(str(outside), s / "to_outside")
    git(s, "add", "-A")
    git(s, "commit", "-q", "-m", "links")
    branch = main._ide_working_branch(ORG, ALICE)
    git(s, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    tree = tree_paths(ALICE)
    for link in ("to_bob", "to_outside"):
        r = read(ALICE, link)
        assert r.status_code != 200 or "secret" not in r.text
    as_user(ALICE)
    hits = client.get(f"/repo/{REPO}/search", params={"q": "secret"}).json()
    assert hits["results"] == []
    assert "a.py" in tree


def test_view_git_ignores_hooks_and_leaves_fetch_head_alone(world):
    """Whatever can write the shared clone's .git must not run code in the
    API process, and a view fetch must not move the clone's FETCH_HEAD,
    which sync_repo_clone resets to."""
    marker = world.tmp / "hook-ran"
    hooks = world.clone / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "reference-transaction"):
        hook = hooks / name
        hook.write_text(
            f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n", encoding="utf-8"
        )
        hook.chmod(0o755)
    fetch_head = world.clone / ".git" / "FETCH_HEAD"
    assert not fetch_head.exists()

    save(ALICE, "a.py", "alice\n")
    assert read(BOB, "a.py").json() == {"content": "original\n"}

    assert not marker.exists()
    assert not fetch_head.exists()


def test_malformed_view_limits_fall_back_to_the_defaults(monkeypatch):
    monkeypatch.setenv("IDE_VIEW_MAX", "lots")
    monkeypatch.setenv("IDE_VIEW_TTL_HOURS", "a day")
    assert workspace.ide_view_max() == 50
    assert workspace.ide_view_ttl_seconds() == 24 * 3600
    monkeypatch.setenv("IDE_VIEW_MAX", "3")
    monkeypatch.setenv("IDE_VIEW_TTL_HOURS", "1.5")
    assert workspace.ide_view_max() == 3
    assert workspace.ide_view_ttl_seconds() == 1.5 * 3600


def test_no_branch_cache_drops_expired_entries(monkeypatch):
    old = time.monotonic() - workspace.NO_BRANCH_CACHE_SECONDS - 1
    monkeypatch.setattr(workspace, "_no_branch_seen", {("c", "old"): old})

    workspace._note_no_branch(Path("c"), "new")

    assert list(workspace._no_branch_seen) == [(str(Path("c")), "new")]


def test_view_keys_are_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    clone = workspace.get_safe_repo_dir(REPO)
    for bad in ("../x", "ABCDEF123456", "", "abc", "_by_id"):
        with pytest.raises(ValueError):
            workspace.ide_view_dir(clone, bad)
    assert workspace.ide_view_dir(clone, "0123456789ab").name == "0123456789ab"


def test_non_git_directories_are_served_as_before(tmp_path, monkeypatch):
    """No clone yet, or a plain directory: no view, no git, no network."""
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    clone = workspace.get_safe_repo_dir(REPO)
    clone.mkdir(parents=True)
    assert workspace.refresh_ide_view(clone, "0123456789ab", "b", None, REPO) is None
    assert workspace.ide_branch_changes(clone, "0123456789ab") == []
    assert not Path(tmp_path / workspace.IDE_VIEWS_DIR).exists()
