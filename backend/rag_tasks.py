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
* Nothing enqueues this task yet; the caller (round 18) passes a checkout
  already at ``commit_sha``. The task checks that with ``git rev-parse``.

Queue: ``maintenance`` (set on the task, since celery_app's task_routes is
not this module's to edit); every deployment's worker consumes it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
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

    client = _redis()
    key = _index_lock_key(repository_id)
    token = uuid.uuid4().hex
    if not client.set(key, token, nx=True, ex=INDEX_LOCK_TTL_SECONDS):
        busy = RepositoryIndexBusy(f"repository {repository_id} is being indexed")
        # Under a worker this schedules a retry (raising celery's Retry);
        # called directly it re-raises ``busy``.
        raise index_repository_task.retry(
            exc=busy,
            countdown=LOCK_RETRY_COUNTDOWN_SECONDS,
            max_retries=LOCK_MAX_RETRIES,
        )
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
