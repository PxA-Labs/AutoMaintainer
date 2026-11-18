"""Index a repository checkout into code_chunks for retrieval (#526).

``index_repository_task(repository_id, commit_sha, clone_path)`` chunks the
checkout (retrieval.chunk_repository), embeds what is new and writes it to
the code_chunks table with the service role, then deletes the repository's
chunks of every other commit. Off unless RAG_INDEXING_ENABLED is 1, true,
yes or on: indexing sends code to Vertex AI and costs money (docs/RAG.md).

Incremental: a chunk whose content_sha (sha256 of path, symbol and content)
is already stored for the repository, at any commit, reuses that embedding,
so a new commit only pays for the functions that changed. Idempotent: rows
are upserted on the table's unique key with ignore-duplicates, so running
the same commit twice embeds nothing and writes nothing.

The organization is read from the repositories row, never taken from the
caller, so a chunk cannot be filed under another tenant. Rows of the
repository filed under another organization (it was transferred) are
deleted before writing, so any run, even of the same commit, moves them.

Trade-offs:

* New rows are written before the old commit's rows are deleted, so a
  query in between can see both commits (duplicates, never nothing).
* One index run per repository at a time: each run deletes every other
  commit's rows, so two runs for different commits interleaved could leave
  the repository with no chunks. The task takes a Redis lock of its own
  (not redis_locks' agent-run lock, so indexing never blocks a run) and
  retries later while another run holds it.
* Commit order is not checked: if an older commit is indexed after a newer
  one, the index holds the older one until the next indexing run.
* The caller passes a checkout already at ``commit_sha``; the task checks
  that with ``git rev-parse``.

What triggers indexing: an agent run starting (``task_prerun`` of
``tasks.run_agent_loop_task``) enqueues ``ensure_repository_indexed_task``,
at most once per repository every REQUEST_DEDUP_SECONDS. It indexes a
fresh shallow clone of the default branch when the index does not already
hold its head (see the section below).

Queue: ``maintenance`` (set on the task, since celery_app's task_routes is
not this module's to edit); every deployment's worker consumes it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from automaintainer._runtime.retrieval import (
    CodeChunk,
    Embedder,
    VertexEmbedder,
    chunk_repository,
)

try:
    from celery import shared_task
except ImportError:  # pragma: no cover - celery is in the server extra

    def shared_task(*args, **kwargs):
        def decorator(fn):
            fn.run = fn
            fn.delay = fn
            fn.apply_async = fn
            return fn

        if len(args) == 1 and callable(args[0]):
            return decorator(args[0])
        return decorator


logger = logging.getLogger(__name__)

RAG_INDEXING_ENV = "RAG_INDEXING_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# A full SHA-1 commit id (lower-cased before the check): it is compared with
# `git rev-parse HEAD` and stored, so an abbreviated sha is refused.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

TABLE = "code_chunks"
ON_CONFLICT = "repository_id,commit_sha,path,start_line,content_sha"
# Above this the repository is skipped, not partly indexed: a cost guard
# (each chunk is one embedding input). ~20k chunks is a large monorepo.
# RAG_MAX_CHUNKS overrides it, read when indexing (max_chunks_per_repository).
MAX_CHUNKS_PER_REPOSITORY = 20000
WRITE_BATCH_SIZE = 200  # rows per upsert request (768 floats each)
LOOKUP_BATCH_SIZE = 100  # content_sha values per "in" filter (URL length)


# The per-repository indexing lock outlives a slow run rather than expire
# under it; a crashed worker's lock frees itself after this.
INDEX_LOCK_TTL_SECONDS = 3600
LOCK_RETRY_COUNTDOWN_SECONDS = 60
LOCK_MAX_RETRIES = 30

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class RepositoryIndexBusy(RuntimeError):
    """Another index run of the same repository holds the lock."""


def rag_indexing_enabled() -> bool:
    return os.getenv(RAG_INDEXING_ENV, "").strip().lower() in _TRUTHY


def max_chunks_per_repository() -> int:
    """RAG_MAX_CHUNKS if it is a positive integer, else the default.

    Read lazily: celery_app imports this module in every worker, so a
    malformed value must not stop workers from booting.
    """
    raw = os.getenv("RAG_MAX_CHUNKS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        if raw:
            logger.warning(
                "RAG_MAX_CHUNKS=%r is not a positive integer; using %d",
                raw,
                MAX_CHUNKS_PER_REPOSITORY,
            )
        return MAX_CHUNKS_PER_REPOSITORY
    return value


def _redis():
    from redis_locks import get_redis_client

    return get_redis_client()


def _index_lock_key(repository_id) -> str:
    return f"automaintainer:rag-index-lock:{repository_id}"


def _supabase():
    """The service-role client the other workers use (tasks.get_supabase)."""
    from tasks import get_supabase

    return get_supabase()


def _embedder() -> Embedder:
    return VertexEmbedder()


def _org_of(sb, repository_id: int) -> str:
    rows = (
        sb.table("repositories")
        .select("id, org_id")
        .eq("id", repository_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise LookupError(f"repository {repository_id} is not registered")
    return rows[0]["org_id"]


def _stored(sb, repository_id: int, shas: list[str]) -> dict[str, dict]:
    """{content_sha: a stored row} for the shas the repository already has."""
    found: dict[str, dict] = {}
    for i in range(0, len(shas), LOOKUP_BATCH_SIZE):
        rows = (
            sb.table(TABLE)
            .select("content_sha, org_id, commit_sha, path, start_line, embedding")
            .eq("repository_id", repository_id)
            .in_("content_sha", shas[i : i + LOOKUP_BATCH_SIZE])
            .execute()
            .data
        )
        for row in rows or []:
            found.setdefault(row["content_sha"], row)
    return found


def _vector(value) -> list[float]:
    # PostgREST returns a pgvector column as its text form, "[0.1,0.2,...]".
    return json.loads(value) if isinstance(value, str) else list(value)


def index_repository(
    sb,
    embedder: Embedder,
    *,
    repository_id: int,
    commit_sha: str,
    clone_path: Path | str,
) -> dict:
    """Index ``clone_path`` as ``repository_id`` at ``commit_sha``; a summary."""
    if not isinstance(commit_sha, str) or not _SHA_RE.match(commit_sha.lower()):
        raise ValueError(f"not a full commit sha: {commit_sha!r}")
    commit_sha = commit_sha.lower()
    root = Path(clone_path)
    if not root.is_dir():
        raise ValueError(f"clone path {str(root)!r} is not a directory")
    repository_id = int(repository_id)
    org_id = _org_of(sb, repository_id)

    # One row per unique key; identical spans at one line collapse.
    chunks: dict[tuple, CodeChunk] = {}
    for chunk in chunk_repository(root):
        chunks.setdefault((chunk.path, chunk.start_line, chunk.content_sha), chunk)
    limit = max_chunks_per_repository()
    if len(chunks) > limit:
        reason = f"{len(chunks)} chunks exceeds RAG_MAX_CHUNKS ({limit})"
        logger.warning("rag index of repository %s skipped: %s", repository_id, reason)
        return {"status": "skipped", "repository_id": repository_id, "reason": reason}

    ordered = list(chunks.values())
    stored = _stored(sb, repository_id, sorted({c.content_sha for c in ordered}))
    present = {
        (row["path"], row["start_line"], sha)
        for sha, row in stored.items()
        if row.get("commit_sha") == commit_sha and row.get("org_id") == org_id
    }

    to_embed = [c for c in ordered if c.content_sha not in stored]
    # Different chunks can share a sha (same text at two lines): embed once.
    unique_texts = list(dict.fromkeys(c.embedding_text for c in to_embed))
    vectors = embedder.embed_documents(unique_texts) if unique_texts else []
    by_text = dict(zip(unique_texts, vectors))

    rows = []
    for c in ordered:
        if (c.path, c.start_line, c.content_sha) in present:
            continue
        if c.content_sha in stored:
            embedding = _vector(stored[c.content_sha]["embedding"])
        else:
            embedding = by_text[c.embedding_text]
        rows.append(
            {
                "org_id": org_id,
                "repository_id": repository_id,
                "commit_sha": commit_sha,
                "path": c.path,
                "symbol": c.symbol,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "content": c.content,
                "content_sha": c.content_sha,
                "embedding": [float(x) for x in embedding],
            }
        )
    # Rows filed under another organization (the repository was moved since
    # it was last indexed) go before the upsert: they would otherwise stay
    # readable by the old organization's members, and with ignore-duplicates
    # they would also block the new rows of an already indexed commit. Their
    # embeddings were read above and are reused.
    moved = (
        sb.table(TABLE)
        .delete()
        .eq("repository_id", repository_id)
        .neq("org_id", org_id)
        .execute()
        .data
    )
    for i in range(0, len(rows), WRITE_BATCH_SIZE):
        sb.table(TABLE).upsert(
            rows[i : i + WRITE_BATCH_SIZE],
            on_conflict=ON_CONFLICT,
            ignore_duplicates=True,
            returning="minimal",
        ).execute()

    deleted = (
        sb.table(TABLE)
        .delete()
        .eq("repository_id", repository_id)
        .neq("commit_sha", commit_sha)
        .execute()
        .data
    )
    result = {
        "status": "indexed",
        "repository_id": repository_id,
        "commit_sha": commit_sha,
        "chunks": len(ordered),
        "embedded": len(to_embed),
        "reused": len(ordered) - len(to_embed),
        "deleted": len(moved or []) + len(deleted or []),
    }
    logger.info("rag index: %s", result)
    return result


def _checkout_head(clone_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(clone_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _index_locked(repository_id: int, commit_sha: str, root: Path) -> dict:
    """:func:`index_repository` under the per-repository Redis lock.

    Raises RepositoryIndexBusy, without indexing, while another run of the
    same repository holds the lock.
    """
    client = _redis()
    key = _index_lock_key(repository_id)
    token = uuid.uuid4().hex
    if not client.set(key, token, nx=True, ex=INDEX_LOCK_TTL_SECONDS):
        raise RepositoryIndexBusy(f"repository {repository_id} is being indexed")
    try:
        return index_repository(
            _supabase(),
            _embedder(),
            repository_id=repository_id,
            commit_sha=commit_sha,
            clone_path=root,
        )
    finally:
        # Compare-and-delete: never free a lock that expired and was taken
        # by another run meanwhile.
        client.eval(_RELEASE_SCRIPT, 1, key, token)


@shared_task(
    name="rag_tasks.index_repository_task",
    queue="maintenance",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    max_retries=3,
)
def index_repository_task(repository_id: int, commit_sha: str, clone_path: str) -> dict:
    """Celery entry point: :func:`index_repository` behind RAG_INDEXING_ENABLED."""
    if not rag_indexing_enabled():
        return {"status": "disabled"}
    root = Path(clone_path)
    if not root.is_dir():
        raise ValueError(f"clone path {clone_path!r} is not a directory")
    head = _checkout_head(root)
    if not isinstance(commit_sha, str) or head != commit_sha.lower():
        raise ValueError(f"checkout HEAD is {head}, not {commit_sha}")
    try:
        return _index_locked(repository_id, commit_sha, root)
    except RepositoryIndexBusy as busy:
        # Under a worker this schedules a retry (raising celery's Retry);
        # called directly it re-raises ``busy``.
        raise index_repository_task.retry(
            exc=busy,
            countdown=LOCK_RETRY_COUNTDOWN_SECONDS,
            max_retries=LOCK_MAX_RETRIES,
        )


# --------------------------------------------------------------------------
# When a repository is indexed: when one of its runs starts (round 18)
# --------------------------------------------------------------------------
#
# A task_prerun handler sees every agent run start in its worker (this
# module is in celery_app's include list, so every worker loads it) and,
# with RAG_INDEXING_ENABLED, enqueues ensure_repository_indexed_task on the
# maintenance queue. That task compares the default branch's head with the
# indexed commit and indexes a fresh shallow clone when they differ. The
# run's own clone is not used: maintenance workers do not share the agent
# workers' workspace, and later runs move that clone while indexing reads it.
#
# Trade-offs: the run that triggered indexing does not wait for it (its
# prompts use whatever is indexed already, or nothing); the default branch
# is what gets indexed, so pull request heads are not.

RUN_TASK_NAME = "tasks.run_agent_loop_task"
# run_agent_loop_task's positional order (main.py /start sends positional
# args): run_id, repo_name, org_id, user_id, repository_id, ...
_RUN_REPOSITORY_ID_ARG = 4
# At most one check per repository in this window, however many runs start.
REQUEST_DEDUP_SECONDS = 600
CLONE_TIMEOUT_SECONDS = 600
LS_REMOTE_TIMEOUT_SECONDS = 60
_FULL_NAME_RE = re.compile(
    r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*$"
)
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*$")


def _request_key(repository_id) -> str:
    return f"automaintainer:rag-index-requested:{repository_id}"


def _run_repository_id(args, kwargs):
    value = (kwargs or {}).get("repository_id")
    if value is None and args and len(args) > _RUN_REPOSITORY_ID_ARG:
        value = args[_RUN_REPOSITORY_ID_ARG]
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def request_index(repository_id: int) -> bool:
    """Enqueue an index check of ``repository_id`` unless one was requested
    in the last REQUEST_DEDUP_SECONDS; True when one was enqueued. Never
    raises: a broker or Redis failure is logged (by type) and the run goes
    on."""
    key = _request_key(repository_id)
    token = uuid.uuid4().hex
    client = None
    try:
        client = _redis()
        if not client.set(key, token, nx=True, ex=REQUEST_DEDUP_SECONDS):
            return False
        ensure_repository_indexed_task.apply_async(args=[repository_id])
        return True
    except Exception as e:
        logger.warning(
            "could not request a rag index check of repository %s (%s)",
            repository_id,
            type(e).__name__,
        )
        if client is not None:
            try:
                client.eval(_RELEASE_SCRIPT, 1, key, token)
            except Exception:  # pragma: no cover - Redis itself is down
                pass
        return False


def _on_task_prerun(sender=None, task_id=None, task=None, args=None, kwargs=None, **_):
    """task_prerun: an agent run of a repository is starting."""
    if getattr(task, "name", None) != RUN_TASK_NAME or not rag_indexing_enabled():
        return
    repository_id = _run_repository_id(args, kwargs)
    if repository_id is not None:
        request_index(repository_id)


try:
    from celery.signals import task_prerun

    task_prerun.connect(_on_task_prerun, weak=False)
except ImportError:  # pragma: no cover - celery is in the server extra
    pass


def _repository(sb, repository_id: int) -> dict | None:
    rows = (
        sb.table("repositories")
        .select("id, org_id, full_name, github_installation_id, default_branch")
        .eq("id", repository_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _indexed_commit(sb, repository_id: int, org_id: str) -> str | None:
    """The commit indexed for the repository under its current org."""
    rows = (
        sb.table(TABLE)
        .select("commit_sha")
        .eq("repository_id", repository_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["commit_sha"] if rows else None


def _credentials(installation_id, *, purpose: str, repository: str):
    from automaintainer._runtime.github_app import credentials_for_installation

    return credentials_for_installation(
        installation_id, purpose=purpose, repository=repository
    )


def _git_env(token) -> dict:
    from automaintainer._runtime.workspace import git_auth_env

    return {**git_auth_env(token), "GIT_LFS_SKIP_SMUDGE": "1"}


def _ls_remote_head(url: str, branch: str, token) -> str:
    """The commit ``branch`` points to on the remote (one HTTPS round trip)."""
    out = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", "ls-remote", "--exit-code", "--", url, f"refs/heads/{branch}"],
        env=_git_env(token),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=LS_REMOTE_TIMEOUT_SECONDS,
        check=True,
    ).stdout
    return out.split()[0].lower() if out.strip() else ""


def _shallow_clone(url: str, branch: str, token, dest: Path) -> Path:
    subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", "clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags"]
        + ["--branch", branch, "--", url, str(dest)],
        env=_git_env(token),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=CLONE_TIMEOUT_SECONDS,
        check=True,
    )
    return dest


def ensure_repository_indexed(repository_id: int) -> dict:
    """Index the default branch's head of ``repository_id`` unless the index
    already holds it; a summary (``fresh``, ``busy``, ``unknown`` or the
    :func:`index_repository` result)."""
    from automaintainer._runtime.workspace import github_remote_url

    sb = _supabase()
    repo = _repository(sb, repository_id)
    if repo is None:
        return {"status": "unknown", "repository_id": repository_id}
    full_name = repo.get("full_name") or ""
    branch = repo.get("default_branch") or "main"  # the column's default
    if not _FULL_NAME_RE.match(full_name) or ".." in full_name:
        raise ValueError(f"repository {repository_id} has an unusable name")
    if not _BRANCH_RE.match(branch) or ".." in branch or "//" in branch:
        raise ValueError(f"repository {repository_id} has an unusable default branch")
    creds = _credentials(
        repo.get("github_installation_id"),
        purpose=f"rag index of {full_name}",
        repository=full_name,
    )
    token = creds.token()
    url = github_remote_url(full_name)
    head = _ls_remote_head(url, branch, token)
    if head and _indexed_commit(sb, repository_id, repo["org_id"]) == head:
        return {"status": "fresh", "repository_id": repository_id, "commit_sha": head}

    workdir = tempfile.mkdtemp(prefix="automaintainer-rag-")
    try:
        root = _shallow_clone(url, branch, token, Path(workdir) / "repo")
        # The branch may have moved since ls-remote: index what was cloned.
        cloned = _checkout_head(root)
        try:
            return _index_locked(repository_id, cloned, root)
        except RepositoryIndexBusy:
            # Another index run of this repository is in progress; the next
            # run start checks again.
            return {"status": "busy", "repository_id": repository_id}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@shared_task(
    name="rag_tasks.ensure_repository_indexed_task",
    queue="maintenance",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    max_retries=3,
)
def ensure_repository_indexed_task(repository_id: int) -> dict:
    """Celery entry point: :func:`ensure_repository_indexed` behind
    RAG_INDEXING_ENABLED."""
    if not rag_indexing_enabled():
        return {"status": "disabled"}
    return ensure_repository_indexed(int(repository_id))
