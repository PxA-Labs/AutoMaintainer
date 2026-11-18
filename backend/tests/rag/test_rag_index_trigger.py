"""When a repository gets indexed (#526): when one of its runs starts.

A Celery ``task_prerun`` handler in rag_tasks sees every agent run start
(``tasks.run_agent_loop_task``) and, only with RAG_INDEXING_ENABLED, enqueues
``ensure_repository_indexed_task`` on the maintenance queue (at most once per
repository every REQUEST_DEDUP_SECONDS). That task compares the default
branch's head (``git ls-remote``) with the commit the index holds and, when
they differ, shallow-clones the branch into a throwaway directory and indexes
it under the per-repository lock. tasks.py is not touched.

No network: git, the GitHub credentials, Redis and Supabase are fakes.
"""

import subprocess
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_redis import FakeRedis
from fake_supabase import FakeSupabase

import rag_tasks

ORG = "aaaaaaaa-0000-4000-8000-0000000000a0"
REPO_ID = 1002
OLD = "1" * 40
HEAD = "2" * 40
REPO_ROW = {
    "id": REPO_ID,
    "org_id": ORG,
    "full_name": "acme/widgets",
    "github_installation_id": 77,
    "default_branch": "trunk",
}


@pytest.fixture(autouse=True)
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rag_tasks, "_redis", lambda: fake)
    return fake


@pytest.fixture
def enqueued(monkeypatch):
    sent = []

    def apply_async(args=None, kwargs=None, **options):
        sent.append((tuple(args or ()), dict(kwargs or {})))

    monkeypatch.setattr(
        rag_tasks.ensure_repository_indexed_task, "apply_async", apply_async
    )
    return sent


def run_task():
    return SimpleNamespace(name=rag_tasks.RUN_TASK_NAME)


# --- The run-start hook --------------------------------------------------------------


def test_the_hook_is_connected_to_task_prerun():
    from celery.signals import task_prerun

    receivers = [
        r() if isinstance(r, weakref.ReferenceType) else r
        for _, r in task_prerun.receivers
    ]
    assert rag_tasks._on_task_prerun in receivers


def test_disabled_by_default_nothing_is_enqueued(monkeypatch, enqueued):
    monkeypatch.delenv("RAG_INDEXING_ENABLED", raising=False)
    rag_tasks._on_task_prerun(task=run_task(), kwargs={"repository_id": REPO_ID})
    assert enqueued == []


@pytest.mark.parametrize(
    "args, kwargs",
    [
        ((), {"repository_id": REPO_ID, "run_id": "r"}),  # tasks.start_agent_task
        (("run", "acme/widgets", ORG, "user", REPO_ID, 77), {}),  # main.py /start
    ],
)
def test_a_run_start_enqueues_one_index_check(
    monkeypatch, enqueued, redis, args, kwargs
):
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "true")
    rag_tasks._on_task_prerun(task=run_task(), args=args, kwargs=kwargs)
    assert enqueued == [((REPO_ID,), {})]
    assert (
        redis.ttls[rag_tasks._request_key(REPO_ID)] == rag_tasks.REQUEST_DEDUP_SECONDS
    )


def test_repeated_run_starts_are_deduplicated(monkeypatch, enqueued):
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "1")
    for _ in range(3):
        rag_tasks._on_task_prerun(task=run_task(), kwargs={"repository_id": REPO_ID})
    rag_tasks._on_task_prerun(task=run_task(), kwargs={"repository_id": 5})
    assert enqueued == [((REPO_ID,), {}), ((5,), {})]


def test_other_tasks_are_ignored(monkeypatch, enqueued):
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "1")
    other = SimpleNamespace(name="tasks.cleanup_stale_runs")
    rag_tasks._on_task_prerun(task=other, kwargs={"repository_id": REPO_ID})
    rag_tasks._on_task_prerun(task=None, kwargs={"repository_id": REPO_ID})
    assert enqueued == []


@pytest.mark.parametrize("value", [None, 0, -3, "abc", True])
def test_a_run_without_a_usable_repository_id_is_ignored(monkeypatch, enqueued, value):
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "1")
    rag_tasks._on_task_prerun(task=run_task(), kwargs={"repository_id": value})
    assert enqueued == []


def test_a_broken_broker_never_breaks_the_run(monkeypatch, caplog):
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "1")

    def boom(*a, **k):
        raise ConnectionError("redis://user:secret@host")

    monkeypatch.setattr(rag_tasks.ensure_repository_indexed_task, "apply_async", boom)
    rag_tasks._on_task_prerun(task=run_task(), kwargs={"repository_id": REPO_ID})
    assert "ConnectionError" in caplog.text and "secret" not in caplog.text
    # The dedup key is released, so the next run start asks again.
    assert rag_tasks._redis().get(rag_tasks._request_key(REPO_ID)) is None


def test_the_check_task_goes_to_the_maintenance_queue():
    task = rag_tasks.ensure_repository_indexed_task
    assert task.name == "rag_tasks.ensure_repository_indexed_task"
    assert task.queue == "maintenance"


# --- ensure_repository_indexed -----------------------------------------------------------


class FakeGit:
    """ls-remote answers ``head``; clone writes a checkout at ``cloned``."""

    def __init__(self, head=HEAD, cloned=HEAD):
        self.head, self.cloned = head, cloned
        self.calls = []

    def ls_remote(self, url, branch, token):
        self.calls.append(("ls-remote", url, branch, token))
        return self.head

    def clone(self, url, branch, token, dest: Path):
        self.calls.append(("clone", url, branch, token))
        dest.mkdir(parents=True)
        (dest / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        return dest

    def rev_parse(self, path):
        return self.cloned


@pytest.fixture
def world(monkeypatch):
    sb = FakeSupabase()
    sb.tables["repositories"] = [dict(REPO_ROW)]
    git = FakeGit()
    tokens = []

    def credentials(installation_id, *, purpose, repository=None):
        tokens.append((installation_id, repository))
        return SimpleNamespace(token=lambda: "ghs_test_token")

    indexed = []

    def index(sb_, embedder, *, repository_id, commit_sha, clone_path):
        indexed.append((repository_id, commit_sha, Path(clone_path)))
        assert Path(clone_path, "mod.py").is_file()  # still there while indexing
        return {"status": "indexed", "commit_sha": commit_sha}

    monkeypatch.setenv("RAG_INDEXING_ENABLED", "1")
    monkeypatch.setattr(rag_tasks, "_supabase", lambda: sb)
    monkeypatch.setattr(rag_tasks, "_credentials", credentials)
    monkeypatch.setattr(rag_tasks, "_ls_remote_head", git.ls_remote)
    monkeypatch.setattr(rag_tasks, "_shallow_clone", git.clone)
    monkeypatch.setattr(rag_tasks, "_checkout_head", git.rev_parse)
    monkeypatch.setattr(rag_tasks, "_embedder", lambda: object())
    monkeypatch.setattr(rag_tasks, "index_repository", index)
    return SimpleNamespace(sb=sb, git=git, tokens=tokens, indexed=indexed)


def chunk_row(commit, org=ORG):
    return {"org_id": org, "repository_id": REPO_ID, "commit_sha": commit}


def test_a_stale_index_is_rebuilt_from_a_fresh_shallow_clone(world):
    world.sb.tables["code_chunks"] = [chunk_row(OLD)]

    result = rag_tasks.ensure_repository_indexed_task(REPO_ID)

    assert result == {"status": "indexed", "commit_sha": HEAD}
    url = "https://github.com/acme/widgets.git"
    assert world.git.calls == [
        ("ls-remote", url, "trunk", "ghs_test_token"),
        ("clone", url, "trunk", "ghs_test_token"),
    ]
    assert world.tokens == [(77, "acme/widgets")]
    ((repo, sha, path),) = world.indexed
    assert (repo, sha) == (REPO_ID, HEAD)
    assert not path.exists()  # the throwaway clone is deleted


def test_a_repository_never_indexed_is_indexed(world):
    assert rag_tasks.ensure_repository_indexed_task(REPO_ID)["status"] == "indexed"


def test_an_index_at_the_head_is_left_alone(world):
    world.sb.tables["code_chunks"] = [chunk_row(HEAD)]
    result = rag_tasks.ensure_repository_indexed_task(REPO_ID)
    assert result == {"status": "fresh", "repository_id": REPO_ID, "commit_sha": HEAD}
    assert [c[0] for c in world.git.calls] == ["ls-remote"]
    assert world.indexed == []


def test_rows_of_another_org_do_not_count_as_indexed(world):
    """A transferred repository's leftovers are not "fresh": index it again."""
    world.sb.tables["code_chunks"] = [chunk_row(HEAD, org="someone-else")]
    assert rag_tasks.ensure_repository_indexed_task(REPO_ID)["status"] == "indexed"


def test_the_commit_actually_cloned_is_what_gets_indexed(world):
    """The branch moved between ls-remote and the clone: index what is on disk."""
    world.git.cloned = "3" * 40
    rag_tasks.ensure_repository_indexed_task(REPO_ID)
    assert world.indexed[0][1] == "3" * 40


def test_a_held_index_lock_skips_without_retrying(world, redis):
    redis.set(rag_tasks._index_lock_key(REPO_ID), "someone", nx=True)
    result = rag_tasks.ensure_repository_indexed_task(REPO_ID)
    assert result == {"status": "busy", "repository_id": REPO_ID}
    assert world.indexed == []


def test_the_clone_is_deleted_when_indexing_fails(world, monkeypatch, redis):
    paths = []

    def fail(sb_, embedder, *, repository_id, commit_sha, clone_path):
        paths.append(Path(clone_path))
        raise RuntimeError("vertex down")

    monkeypatch.setattr(rag_tasks, "index_repository", fail)
    with pytest.raises(RuntimeError):
        rag_tasks.ensure_repository_indexed_task(REPO_ID)
    assert paths and not paths[0].exists()
    assert rag_tasks._index_lock_key(REPO_ID) not in redis.store


def test_disabled_the_check_does_nothing(world, monkeypatch):
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "0")
    assert rag_tasks.ensure_repository_indexed_task(REPO_ID) == {"status": "disabled"}
    assert world.git.calls == []


def test_an_unknown_repository_is_skipped(world):
    world.sb.tables["repositories"] = []
    result = rag_tasks.ensure_repository_indexed_task(REPO_ID)
    assert result == {"status": "unknown", "repository_id": REPO_ID}


@pytest.mark.parametrize("branch", ["-x", "a b", "", None, "../x", "x..y", "a//b"])
def test_an_unsafe_default_branch_is_refused(world, branch):
    world.sb.tables["repositories"][0]["default_branch"] = branch
    if not branch:
        # NULL (or empty) in the database means the column default, "main".
        assert rag_tasks.ensure_repository_indexed_task(REPO_ID)["status"] == "indexed"
        assert world.git.calls[0][2] == "main"
        return
    with pytest.raises(ValueError):
        rag_tasks.ensure_repository_indexed_task(REPO_ID)
    assert world.git.calls == []


@pytest.mark.parametrize("full_name", ["acme", "acme/../x", "-acme/w", "a/b/c"])
def test_an_unsafe_repository_name_is_refused(world, full_name):
    world.sb.tables["repositories"][0]["full_name"] = full_name
    with pytest.raises(ValueError):
        rag_tasks.ensure_repository_indexed_task(REPO_ID)
    assert world.git.calls == []


def test_the_index_task_still_retries_while_the_lock_is_held(monkeypatch, redis):
    """index_repository_task keeps its old behaviour after the refactor."""
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "1")
    redis.set(rag_tasks._index_lock_key(REPO_ID), "someone", nx=True)
    monkeypatch.setattr(rag_tasks, "_checkout_head", lambda root: HEAD)
    with pytest.raises(rag_tasks.RepositoryIndexBusy):
        rag_tasks.index_repository_task(REPO_ID, HEAD, str(Path(__file__).parent))


# --- The git helpers, against a local repository -----------------------------------


@pytest.fixture
def origin(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--quiet", "--initial-branch=trunk")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git("add", "mod.py")
    git("commit", "--quiet", "-m", "one")
    return repo, git("rev-parse", "HEAD")


def test_ls_remote_and_shallow_clone_agree(origin, tmp_path):
    repo, sha = origin
    url = repo.as_uri()
    assert rag_tasks._ls_remote_head(url, "trunk", None) == sha
    dest = rag_tasks._shallow_clone(url, "trunk", None, tmp_path / "copy")
    assert rag_tasks._checkout_head(dest) == sha
    assert (dest / "mod.py").is_file()


def test_ls_remote_of_a_missing_branch_fails(origin):
    repo, _ = origin
    with pytest.raises(subprocess.CalledProcessError):
        rag_tasks._ls_remote_head(repo.as_uri(), "nope", None)
