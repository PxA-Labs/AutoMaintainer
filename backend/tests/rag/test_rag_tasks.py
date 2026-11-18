"""The repository indexing task (#526): incremental, idempotent, off by default."""

import subprocess

import pytest
from fake_supabase import FakeSupabase

import rag_tasks
from automaintainer._runtime.retrieval import HashingEmbedder

ORG = "aaaaaaaa-0000-4000-8000-0000000000a0"
REPO_ID = 1002
SHA1 = "1" * 40
SHA2 = "2" * 40


class CountingEmbedder(HashingEmbedder):
    def __init__(self):
        super().__init__()
        self.embedded = []

    def embed_documents(self, texts):
        self.embedded.extend(texts)
        return super().embed_documents(texts)


@pytest.fixture
def sb():
    db = FakeSupabase()
    db.tables["repositories"] = [{"id": REPO_ID, "org_id": ORG}]
    return db


def _write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


FILES = {
    "pkg/a.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n",
    "README.md": "# Demo\n",
}


def _rows(sb, **match):
    return [
        r
        for r in sb.tables.get("code_chunks", [])
        if all(r[k] == v for k, v in match.items())
    ]


def test_indexes_every_chunk_with_the_repositorys_org(sb, tmp_path):
    _write(tmp_path, FILES)
    e = CountingEmbedder()
    result = rag_tasks.index_repository(
        sb, e, repository_id=REPO_ID, commit_sha=SHA1, clone_path=tmp_path
    )
    rows = _rows(sb)
    assert {(r["path"], r["symbol"]) for r in rows} == {
        ("pkg/a.py", "a"),
        ("pkg/a.py", "b"),
        ("README.md", None),
    }
    assert all(r["org_id"] == ORG and r["commit_sha"] == SHA1 for r in rows)
    assert all(len(r["content_sha"]) == 64 for r in rows)
    assert result == {
        "status": "indexed",
        "repository_id": REPO_ID,
        "commit_sha": SHA1,
        "chunks": 3,
        "embedded": 3,
        "reused": 0,
        "deleted": 0,
    }
    assert len(e.embedded) == 3


def test_reindexing_the_same_commit_is_idempotent_and_embeds_nothing(sb, tmp_path):
    _write(tmp_path, FILES)
    rag_tasks.index_repository(
        sb,
        HashingEmbedder(),
        repository_id=REPO_ID,
        commit_sha=SHA1,
        clone_path=tmp_path,
    )
    before = [dict(r) for r in _rows(sb)]
    e = CountingEmbedder()
    result = rag_tasks.index_repository(
        sb, e, repository_id=REPO_ID, commit_sha=SHA1, clone_path=tmp_path
    )
    assert e.embedded == []
    assert result["embedded"] == 0 and result["reused"] == 3 and result["deleted"] == 0
    assert _rows(sb) == before


def test_new_commit_reuses_unchanged_embeddings_and_drops_older_commits(sb, tmp_path):
    _write(tmp_path, FILES)
    rag_tasks.index_repository(
        sb,
        HashingEmbedder(),
        repository_id=REPO_ID,
        commit_sha=SHA1,
        clone_path=tmp_path,
    )
    old = {r["symbol"]: r for r in _rows(sb)}

    # b() changes; a() and the README do not.
    _write(tmp_path, {"pkg/a.py": FILES["pkg/a.py"].replace("return 2", "return 3")})
    e = CountingEmbedder()
    result = rag_tasks.index_repository(
        sb, e, repository_id=REPO_ID, commit_sha=SHA2, clone_path=tmp_path
    )
    assert len(e.embedded) == 1 and "return 3" in e.embedded[0]
    assert (result["embedded"], result["reused"], result["deleted"]) == (1, 2, 3)
    rows = {r["symbol"]: r for r in _rows(sb)}
    assert set(rows) == {"a", "b", None}
    assert {r["commit_sha"] for r in rows.values()} == {SHA2}
    assert rows["b"]["content_sha"] != old["b"]["content_sha"]
    # Reused rows carry the stored embedding unchanged.
    for symbol in ("a", None):
        assert rows[symbol]["embedding"] == old[symbol]["embedding"]
        assert rows[symbol]["content_sha"] == old[symbol]["content_sha"]


def test_only_this_repositorys_chunks_are_deleted(sb, tmp_path):
    sb.tables["code_chunks"] = [
        {
            "org_id": "other",
            "repository_id": 9,
            "commit_sha": "0" * 40,
            "path": "z.py",
            "start_line": 1,
            "content_sha": "x",
        }
    ]
    _write(tmp_path, FILES)
    rag_tasks.index_repository(
        sb,
        HashingEmbedder(),
        repository_id=REPO_ID,
        commit_sha=SHA1,
        clone_path=tmp_path,
    )
    assert len(_rows(sb, repository_id=9)) == 1


def test_unknown_repository_is_refused(sb, tmp_path):
    _write(tmp_path, FILES)
    with pytest.raises(LookupError):
        rag_tasks.index_repository(
            sb, HashingEmbedder(), repository_id=7, commit_sha=SHA1, clone_path=tmp_path
        )
    assert "code_chunks" not in sb.tables


@pytest.mark.parametrize("sha", ["", "HEAD", "zz" * 20, "1" * 41, "abc; rm -rf /"])
def test_bad_commit_sha_is_refused(sb, tmp_path, sha):
    with pytest.raises(ValueError):
        rag_tasks.index_repository(
            sb,
            HashingEmbedder(),
            repository_id=REPO_ID,
            commit_sha=sha,
            clone_path=tmp_path,
        )


def test_missing_clone_path_is_refused(sb, tmp_path):
    with pytest.raises(ValueError):
        rag_tasks.index_repository(
            sb,
            HashingEmbedder(),
            repository_id=REPO_ID,
            commit_sha=SHA1,
            clone_path=tmp_path / "missing",
        )


def test_too_many_chunks_is_skipped_without_embedding(sb, tmp_path, monkeypatch):
    monkeypatch.setattr(rag_tasks, "MAX_CHUNKS_PER_REPOSITORY", 2)
    _write(tmp_path, FILES)
    e = CountingEmbedder()
    result = rag_tasks.index_repository(
        sb, e, repository_id=REPO_ID, commit_sha=SHA1, clone_path=tmp_path
    )
    assert result["status"] == "skipped" and "3 chunks" in result["reason"]
    assert e.embedded == [] and "code_chunks" not in sb.tables


def test_embeddings_are_written_in_batches(sb, tmp_path, monkeypatch):
    monkeypatch.setattr(rag_tasks, "WRITE_BATCH_SIZE", 2)
    _write(tmp_path, FILES)
    rag_tasks.index_repository(
        sb,
        HashingEmbedder(),
        repository_id=REPO_ID,
        commit_sha=SHA1,
        clone_path=tmp_path,
    )
    upserts = [c for c in sb.calls if c == ("code_chunks", "upsert")]
    assert len(upserts) == 2


# --------------------------------------------------------------------------
# The Celery task
# --------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    _write(tmp_path, FILES)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "init",
    )
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


@pytest.mark.parametrize("value", [None, "", "false", "0", "no"])
def test_task_is_disabled_unless_rag_indexing_is_enabled(monkeypatch, sb, clone, value):
    path, sha = clone
    if value is None:
        monkeypatch.delenv("RAG_INDEXING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("RAG_INDEXING_ENABLED", value)
    monkeypatch.setattr(rag_tasks, "_supabase", lambda: pytest.fail("no DB call"))
    monkeypatch.setattr(rag_tasks, "_embedder", lambda: pytest.fail("no embedder"))
    assert rag_tasks.index_repository_task.run(REPO_ID, sha, str(path)) == {
        "status": "disabled"
    }


@pytest.mark.parametrize("value", ["true", "1", "YES", "on"])
def test_task_indexes_when_enabled(monkeypatch, sb, clone, value):
    path, sha = clone
    monkeypatch.setenv("RAG_INDEXING_ENABLED", value)
    monkeypatch.setattr(rag_tasks, "_supabase", lambda: sb)
    monkeypatch.setattr(rag_tasks, "_embedder", HashingEmbedder)
    result = rag_tasks.index_repository_task.run(REPO_ID, sha, str(path))
    assert result["status"] == "indexed" and result["chunks"] == 3
    assert {r["commit_sha"] for r in _rows(sb)} == {sha}


def test_task_refuses_a_checkout_at_another_commit(monkeypatch, sb, clone):
    path, sha = clone
    monkeypatch.setenv("RAG_INDEXING_ENABLED", "true")
    monkeypatch.setattr(rag_tasks, "_supabase", lambda: sb)
    monkeypatch.setattr(rag_tasks, "_embedder", HashingEmbedder)
    with pytest.raises(ValueError, match="HEAD"):
        rag_tasks.index_repository_task.run(REPO_ID, "f" * 40, str(path))
    assert "code_chunks" not in sb.tables


def test_task_is_registered_with_the_worker_and_goes_to_the_maintenance_queue():
    """Workers consume named queues only (-Q), so an unrouted task would sit
    in Celery's default "celery" queue forever."""
    from celery_app import celery_app

    assert "rag_tasks" in celery_app.conf.include
    celery_app.loader.import_default_modules()
    task = celery_app.tasks["rag_tasks.index_repository_task"]
    assert task.queue == "maintenance"
