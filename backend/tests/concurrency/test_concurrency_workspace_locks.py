"""Clone locks and the workspace scrub across engines and threads (workspace).

Clone locks are per process and keyed by the clone's absolute path, which
includes the workspace root: operations on one clone serialise, operations
on clones under different roots (different engines) run in parallel. The
credential scrub runs once per workspace root however many runs of however
many engines start at once.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import workspace
from automaintainer.context import current_engine
from automaintainer.engine import AutoMaintainerEngine

from concurrency_world import REPO, make_config

KEY = "a" * 12  # IDE view key (12 hex chars); not a secret


def _in_engine(config, fn):
    token = current_engine.set(AutoMaintainerEngine(config)._context)
    try:
        return fn()
    finally:
        current_engine.reset(token)


def test_lock_is_per_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_clone_locks", {})
    clone = tmp_path / "ws-a" / "clone"
    same = [
        clone,
        str(clone),
        tmp_path / "ws-a" / "x" / ".." / "clone",
        Path(str(clone) + "/"),
    ]
    assert len({id(workspace._clone_lock(p)) for p in same}) == 1
    other_root = tmp_path / "ws-b" / "clone"
    assert workspace._clone_lock(other_root) is not workspace._clone_lock(clone)


def test_engines_with_different_roots_get_different_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_clone_locks", {})

    def clone_lock():
        return workspace._clone_lock(workspace.get_safe_repo_dir(REPO, 42))

    lock_a = _in_engine(make_config(tmp_path, "a"), clone_lock)
    lock_b = _in_engine(make_config(tmp_path, "b"), clone_lock)
    lock_a2 = _in_engine(make_config(tmp_path, "a"), clone_lock)
    assert lock_a is lock_a2
    assert lock_a is not lock_b


class _RefreshRecorder:
    """Stands in for the git work refresh_ide_view does under the lock."""

    def __init__(self, hold: float = 0.02, rendezvous: int = 0):
        self.guard = threading.Lock()
        self.inside: dict = {}
        self.max_inside: dict = {}
        self.hold = hold
        # When set, every caller must be inside at the same time.
        self.barrier = threading.Barrier(rendezvous) if rendezvous else None

    def __call__(self, clone_dir, key, branch, remote, env, timeout, state):
        path = str(clone_dir)
        with self.guard:
            self.inside[path] = self.inside.get(path, 0) + 1
            self.max_inside[path] = max(self.max_inside.get(path, 0), self.inside[path])
        try:
            if self.barrier is not None:
                self.barrier.wait(30)
            time.sleep(self.hold)
        finally:
            with self.guard:
                self.inside[path] -= 1
        return Path(clone_dir) / "view"


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(workspace, "_clone_locks", {})
    monkeypatch.setattr(workspace, "is_git_clone", lambda path: True)
    monkeypatch.setattr(workspace, "ide_view_dir", lambda clone, key: None)
    monkeypatch.setattr(workspace, "_ls_remote", lambda *a, **k: None)
    monkeypatch.setattr(workspace, "prune_ide_views", lambda keep=(): [])


def _refresh_all(clones):
    start = threading.Barrier(len(clones))

    def refresh(clone):
        start.wait(30)
        return workspace.refresh_ide_view(clone, KEY, "main", None, REPO)

    with ThreadPoolExecutor(max_workers=len(clones)) as pool:
        return list(pool.map(refresh, clones))


def test_same_clone_serialises(tmp_path, monkeypatch, no_git):
    recorder = _RefreshRecorder()
    monkeypatch.setattr(workspace, "_refresh_locked", recorder)
    clone = tmp_path / "ws-a" / "clone"
    spellings = [clone, str(clone), tmp_path / "ws-a" / "y" / ".." / "clone"] * 2

    views = _refresh_all(spellings)

    assert len(views) == len(spellings)
    assert max(recorder.max_inside.values()) == 1


def test_different_roots_do_not_block_each_other(tmp_path, monkeypatch, no_git):
    """Every refresh must be inside its lock at once (a barrier inside the
    critical section); if any two shared a lock the barrier would break."""
    roots = [tmp_path / f"ws-{n}" for n in "abcd"]
    recorder = _RefreshRecorder(hold=0, rendezvous=len(roots))
    monkeypatch.setattr(workspace, "_refresh_locked", recorder)

    views = _refresh_all([root / "clone" for root in roots])

    assert views == [root / "clone" / "view" for root in roots]
    assert set(recorder.max_inside.values()) == {1}


async def test_scrub_runs_once_per_root_under_concurrency(tmp_path, monkeypatch):
    """Many runs of four engines (two of which share a root) scrub at once:
    each clone is scrubbed exactly once, by a run of an engine on its root."""
    scrubbed = []

    async def scrub(repo_dir):
        scrubbed.append(repo_dir)
        await asyncio.sleep(0.001)

    monkeypatch.setattr(workspace, "scrub_clone_credentials", scrub)
    roots = {n: tmp_path / f"ws-{n}" for n in ("a", "b", "c")}
    for root in roots.values():
        (root / "legacy-clone").mkdir(parents=True)
        (root / workspace.REPO_ID_DIR / "42").mkdir(parents=True)
    configs = [
        make_config(tmp_path, "a"),
        make_config(tmp_path, "b"),
        make_config(tmp_path, "c"),
        make_config(tmp_path, "c2", workspace_dir=roots["c"]),
    ]
    contexts = [AutoMaintainerEngine(c)._context for c in configs]

    async def run_scrub(ctx):
        current_engine.set(ctx)  # this task's own context copy
        await workspace.scrub_workspace_credentials()

    await asyncio.gather(*(run_scrub(contexts[i % 4]) for i in range(40)))

    expected = sorted(
        str(root / sub)
        for root in roots.values()
        for sub in ("legacy-clone", Path(workspace.REPO_ID_DIR) / "42")
    )
    assert sorted(scrubbed) == expected
    assert workspace._scrubbed_roots == {str(r) for r in roots.values()}
    assert current_engine.get() is None
