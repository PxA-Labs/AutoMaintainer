"""Code retrieval over a target repository (#526).

Four pieces, each behind a small seam so round 18 can wire retrieval into the
agents' prompts without touching how it is computed:

* **Chunking** (:func:`chunk_repository`, :func:`chunk_file`): one chunk per
  function, method and class header of a Python/JS/TS file, found with the
  existing tree-sitter indexer (``ast_indexer``, used read-only); other text
  files, and source files with no definitions, become file-level chunks.
  Chunks are capped in lines and characters.
* **Embedding** (:class:`Embedder`): :class:`VertexEmbedder` calls Vertex AI
  ``text-embedding-005`` through google-genai (the optional ``rag`` extra,
  imported lazily) with batching and retries; :class:`HashingEmbedder` is a
  deterministic, offline bag-of-identifiers embedder for tests and the
  offline recall eval. Both produce 768-dimensional unit vectors.
* **Retrieval** (:class:`Retriever`): :class:`PgvectorRetriever` calls the
  ``match_code_chunks`` RPC (supabase/migrations/*_code_chunks_pgvector.sql);
  :class:`InMemoryRetriever` ranks chunks in Python. A retriever's tenant
  scope (org and repository) is fixed when it is built, never per query, so
  a model-facing tool built on it cannot be asked for another tenant's code.
* **Prompt formatting** (:func:`format_untrusted_chunks`): retrieved code is
  written by whoever can push to the target repository, so each chunk is
  wrapped with ``agents.untrusted_block`` under the existing
  ``UNTRUSTED_DATA_NOTICE``.

Nothing here is called by the agents yet. See docs/RAG.md.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence, runtime_checkable

from .ast_indexer import CodebaseMapper, TreeSitterParser

EMBEDDING_DIMENSIONS = 768  # text-embedding-005; the code_chunks column width
DEFAULT_EMBEDDING_MODEL = "text-embedding-005"
DEFAULT_LOCATION = "us-central1"

# Size caps. A chunk longer than MAX_CHUNK_LINES is split into windows of
# that many lines; each window is cut at MAX_CHUNK_CHARS (minified code,
# data blobs). text-embedding-005 reads at most 2048 tokens per input and
# truncates the rest, so ~6000 characters is about what it can use anyway.
MAX_CHUNK_LINES = 150
MAX_CHUNK_CHARS = 6000
MAX_FILE_BYTES = 256_000
TRUNCATED_MARKER = "\n...[truncated]"

# The tree-sitter indexer's own ignore list, plus tool caches it predates.
IGNORED_DIRS = frozenset(CodebaseMapper(".").ignored_dirs) | {
    ".venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    "coverage",
}
# Generated files that would only add noise (and cost) to the index.
SKIPPED_FILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
    }
)
SKIPPED_SUFFIXES = (".min.js", ".min.css", ".map", ".lock")


@dataclass(frozen=True)
class CodeChunk:
    """A span of one file. Lines are 1-based and inclusive."""

    path: str  # repository-relative, POSIX separators
    symbol: str | None  # "Class.method", "func", "Class"; None for file-level
    start_line: int
    end_line: int
    content: str

    @property
    def embedding_text(self) -> str:
        """What is embedded: the path and symbol help match issue text."""
        header = self.path if not self.symbol else f"{self.path} {self.symbol}"
        return f"{header}\n{self.content}"

    @property
    def content_sha(self) -> str:
        """sha256 of :attr:`embedding_text`: equal sha, equal embedding.

        The line numbers are left out on purpose, so a function that only
        moved within its file keeps its embedding on re-indexing.
        """
        return hashlib.sha256(self.embedding_text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

_PARSER: TreeSitterParser | None = None


def _parser() -> TreeSitterParser:
    global _PARSER
    if _PARSER is None:
        _PARSER = TreeSitterParser()
    return _PARSER


def _read_text(path: Path) -> str | None:
    """The file's text, or None for binary, non-UTF-8, empty or huge files."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data or b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n")


def _cap(text: str) -> str:
    if len(text) <= MAX_CHUNK_CHARS:
        return text
    return text[:MAX_CHUNK_CHARS] + TRUNCATED_MARKER


def _windows(path, symbol, lines, start, end) -> list[CodeChunk]:
    """Chunks of lines[start-1:end], at most MAX_CHUNK_LINES lines each."""
    chunks = []
    for lo in range(start, end + 1, MAX_CHUNK_LINES):
        hi = min(lo + MAX_CHUNK_LINES - 1, end)
        content = "\n".join(lines[lo - 1 : hi])
        if content.strip():
            chunks.append(CodeChunk(path, symbol, lo, hi, _cap(content)))
    return chunks


def _with_decorators(lines, start: int) -> int:
    """Move ``start`` up over decorator lines directly above a definition."""
    while start > 1 and lines[start - 2].lstrip().startswith("@"):
        start -= 1
    return start


def _strip_trailing_blank(lines, start: int, end: int) -> int:
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


# ast_indexer slices names out of the decoded source with tree-sitter's
# *byte* offsets, so a multi-byte character above a definition shifts the
# name ("terange(s" for "daterange"). Its line numbers are right; the name is
# taken from the definition line instead, the indexer's kept only when that
# line really contains it.
_NAME_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="),
    re.compile(
        r"^\s*(?:(?:static|async|get|set|public|private|protected|readonly)\s+)*"
        r"\*?\s*([A-Za-z_$#][\w$]*)\s*\("
    ),
)


def _name(lines, item) -> str:
    line = lines[item["start_line"] - 1] if item["start_line"] <= len(lines) else ""
    name = item.get("name") or ""
    if re.fullmatch(r"[A-Za-z_$#][\w$]*", name) and re.search(
        rf"(?<![\w$]){re.escape(name)}(?![\w$])", line
    ):
        return name
    for pattern in _NAME_PATTERNS:
        m = pattern.search(line)
        if m:
            return m.group(1)
    return "<anonymous>"


def _inside(inner, outer) -> bool:
    return (
        outer["start_line"] <= inner["start_line"]
        and inner["end_line"] <= outer["end_line"]
        and inner is not outer
    )


def _definition_chunks(path: str, lines: list[str], parsed: dict) -> list[CodeChunk]:
    classes = parsed["classes"]
    functions = parsed["functions"]
    chunks: list[CodeChunk] = []

    # Functions nested in another function stay in their parent's chunk.
    top_functions = [f for f in functions if not any(_inside(f, g) for g in functions)]
    for func in top_functions:
        owners = [c for c in classes if _inside(func, c)]
        # The innermost enclosing class names the method.
        owner = max(owners, key=lambda c: c["start_line"], default=None)
        name = _name(lines, func)
        symbol = f"{_name(lines, owner)}.{name}" if owner else name
        start = _with_decorators(lines, func["start_line"])
        chunks += _windows(path, symbol, lines, start, func["end_line"])

    # A class contributes its header: everything before its first method
    # (docstring, attributes), so method bodies are not embedded twice.
    for cls in classes:
        members = [
            _with_decorators(lines, f["start_line"])
            for f in top_functions
            if _inside(f, cls)
        ] + [c["start_line"] for c in classes if _inside(c, cls)]
        start = _with_decorators(lines, cls["start_line"])
        end = min(members) - 1 if members else cls["end_line"]
        end = _strip_trailing_blank(lines, start, end)
        chunks += _windows(path, _name(lines, cls), lines, start, end)

    chunks.sort(key=lambda c: (c.start_line, c.end_line, c.symbol or ""))
    return chunks


def _skipped_name(name: str) -> bool:
    return name in SKIPPED_FILE_NAMES or name.endswith(SKIPPED_SUFFIXES)


def chunk_file(repo_dir: Path | str, rel_path: str) -> list[CodeChunk]:
    """The chunks of one file of a checkout (``rel_path`` is POSIX-relative)."""
    root = Path(repo_dir).resolve()
    path = (root / rel_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"{rel_path!r} is outside the repository")
    rel = path.relative_to(root).as_posix()
    if _skipped_name(path.name):
        return []
    text = _read_text(path)
    if text is None:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    if path.suffix in _parser().language_map:
        parsed = _parser().parse_file(str(path))
        if parsed and (parsed["classes"] or parsed["functions"]):
            chunks = _definition_chunks(rel, lines, parsed)
            if chunks:
                return chunks
    return _windows(rel, None, lines, 1, len(lines))


def iter_repository_files(repo_dir: Path | str) -> Iterable[str]:
    """Repository-relative POSIX paths of regular files, sorted.

    Ignored directories are pruned and symbolic links skipped, so a link in
    the target repository cannot pull in files from outside it.
    """
    root = Path(repo_dir)
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in IGNORED_DIRS and not (Path(dirpath) / d).is_symlink()
        ]
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            found.append(full.relative_to(root).as_posix())
    return sorted(found)


def chunk_repository(repo_dir: Path | str) -> list[CodeChunk]:
    """Every chunk of a checkout, in path then line order."""
    chunks: list[CodeChunk] = []
    for rel in iter_repository_files(repo_dir):
        chunks += chunk_file(repo_dir, rel)
    return chunks


# --------------------------------------------------------------------------
# Embedding
# --------------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Turns text into EMBEDDING_DIMENSIONS-wide vectors."""

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


_WORD_RE = re.compile(r"[A-Za-z][a-z0-9]*|[A-Z]+(?![a-z])|[0-9]+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have if in into is it its of on "
    "or so that the their then there these this to was were when which while "
    "will with self def return none true false".split()
)


def _tokens(text: str) -> list[str]:
    """Identifiers and their camelCase/snake_case parts, lower-cased."""
    out = []
    for ident in _IDENT_RE.findall(text):
        lowered = ident.lower()
        parts = [p.lower() for p in _WORD_RE.findall(ident)]
        if len(parts) > 1:
            out.append(lowered.strip("_"))
        out += parts
    return [t for t in out if len(t) > 1 and t not in _STOPWORDS]


class HashingEmbedder:
    """Deterministic, offline embedder: hashed bag of identifier tokens.

    A lexical baseline, not a semantic model: two texts are close when they
    share identifiers or words. It needs no network, no model download and
    no credentials, which is what tests and the offline recall eval need.
    Each token adds +-1 (sign from the hash) at one of ``dimensions``
    positions, weighted by 1 + log(count); the vector is L2-normalised.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS):
        self.dimensions = dimensions

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        counts: dict[str, int] = {}
        for token in _tokens(text):
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            sign = 1.0 if value & 1 else -1.0
            vector[(value >> 1) % self.dimensions] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0.0:
            # A zero vector has no cosine; give empty text a fixed direction.
            vector[0] = 1.0
            return vector
        return [x / norm for x in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


# HTTP statuses worth retrying: rate limit, timeouts, server-side failures.
_RETRYABLE_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    code = getattr(exc, "code", None)
    return isinstance(code, int) and code in _RETRYABLE_CODES


class VertexEmbedder:
    """Vertex AI text embeddings through google-genai (the ``rag`` extra).

    Configuration, each overridable by argument:

    * ``RAG_EMBEDDING_MODEL`` (default ``text-embedding-005``);
    * ``GOOGLE_CLOUD_PROJECT`` (required) and ``GOOGLE_CLOUD_LOCATION``
      (default ``us-central1``): the project billed and the region the code
      is sent to;
    * ``RAG_EMBEDDING_BATCH_SIZE`` (default 16; Vertex accepts at most 250
      inputs and about 20k tokens per request, and chunks run to ~1.5k
      tokens).

    Credentials are application default credentials (on Cloud Run, the
    service account). Transient failures (429, 5xx, connection errors) are
    retried ``max_retries`` times with exponential backoff (1s, 2s, 4s, ...,
    capped at 30s); anything else is raised at once. Documents are embedded
    with task type RETRIEVAL_DOCUMENT and queries with CODE_RETRIEVAL_QUERY.

    google-genai is imported on first use, so importing this module needs
    neither the extra nor credentials.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        project: str | None = None,
        location: str | None = None,
        batch_size: int | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        client=None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = (
            model or os.getenv("RAG_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
        )
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT") or None
        self.location = (
            location or os.getenv("GOOGLE_CLOUD_LOCATION") or DEFAULT_LOCATION
        )
        size = batch_size or int(os.getenv("RAG_EMBEDDING_BATCH_SIZE") or 16)
        self.batch_size = max(1, min(size, 250))
        self.max_retries = max(0, max_retries)
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._client = client
        self._sleep = sleep

    def _get_client(self):
        if self._client is None:
            if not self.project:
                raise RuntimeError(
                    "VertexEmbedder needs a Google Cloud project: set "
                    "GOOGLE_CLOUD_PROJECT (and optionally GOOGLE_CLOUD_LOCATION)"
                )
            from google import genai  # the optional "rag" extra

            self._client = genai.Client(
                vertexai=True, project=self.project, location=self.location
            )
        return self._client

    def _call(self, texts: list[str], task_type: str) -> list[list[float]]:
        client = self._get_client()
        config = {
            "task_type": task_type,
            "output_dimensionality": EMBEDDING_DIMENSIONS,
            "auto_truncate": True,
        }
        attempt = 0
        while True:
            try:
                response = client.models.embed_content(
                    model=self.model, contents=texts, config=config
                )
                break
            except Exception as exc:
                if attempt >= self.max_retries or not _is_retryable(exc):
                    raise
                self._sleep(min(self.backoff_cap, self.backoff_base * 2**attempt))
                attempt += 1
        embeddings = list(getattr(response, "embeddings", None) or [])
        if len(embeddings) != len(texts):
            raise ValueError(
                f"Vertex returned {len(embeddings)} embeddings for {len(texts)} inputs"
            )
        vectors = [list(e.values or []) for e in embeddings]
        for v in vectors:
            if len(v) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"expected {EMBEDDING_DIMENSIONS}-dimensional embeddings, got {len(v)}"
                )
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        texts = list(texts)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            vectors += self._call(texts[i : i + self.batch_size], "RETRIEVAL_DOCUMENT")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._call([text], "CODE_RETRIEVAL_QUERY")[0]


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

MAX_K = 50  # match_code_chunks clamps to the same bound


@dataclass(frozen=True)
class RetrievedChunk:
    path: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    similarity: float


@runtime_checkable
class Retriever(Protocol):
    """The k chunks most relevant to ``query``, best first.

    Implementations are bound to one repository (and organization) when
    built; ``retrieve`` takes no tenant argument on purpose.
    """

    def retrieve(self, query: str, k: int = 5) -> list[RetrievedChunk]: ...


class PgvectorRetriever:
    """Nearest chunks from code_chunks through the ``match_code_chunks`` RPC.

    ``client`` is a supabase-py client. With the service role key RLS is
    bypassed and the function's own org and repository filters keep tenants
    apart; with a user's token RLS applies as well.
    """

    def __init__(self, client, embedder: Embedder, *, org_id: str, repository_id: int):
        if not org_id or not repository_id:
            raise ValueError("PgvectorRetriever needs an org_id and a repository_id")
        self._client = client
        self._embedder = embedder
        self.org_id = str(org_id)
        self.repository_id = int(repository_id)

    def retrieve(self, query: str, k: int = 5) -> list[RetrievedChunk]:
        k = min(int(k), MAX_K)
        if k <= 0:
            return []
        embedding = self._embedder.embed_query(query)
        result = self._client.rpc(
            "match_code_chunks",
            {
                "p_org_id": self.org_id,
                "p_repository_id": self.repository_id,
                "p_query_embedding": [float(x) for x in embedding],
                "p_k": k,
            },
        ).execute()
        return [
            RetrievedChunk(
                path=row["path"],
                symbol=row.get("symbol"),
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                content=row["content"],
                similarity=float(row["similarity"]),
            )
            for row in (result.data or [])
        ]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


class InMemoryRetriever:
    """Exact cosine ranking over chunks held in memory (tests, offline eval).

    Ties are broken by path, then start line, so results are deterministic.
    """

    def __init__(self, chunks: Sequence[CodeChunk], embedder: Embedder):
        self._chunks = list(chunks)
        self._embedder = embedder
        self._vectors = embedder.embed_documents(
            [c.embedding_text for c in self._chunks]
        )

    def retrieve(self, query: str, k: int = 5) -> list[RetrievedChunk]:
        k = min(int(k), MAX_K)
        if k <= 0 or not self._chunks:
            return []
        q = self._embedder.embed_query(query)
        scored = sorted(
            (
                (-_cosine(q, v), c.path, c.start_line, c)
                for c, v in zip(self._chunks, self._vectors)
            ),
            key=lambda t: t[:3],
        )
        return [
            RetrievedChunk(c.path, c.symbol, c.start_line, c.end_line, c.content, -s)
            for s, _, _, c in scored[:k]
        ]


# --------------------------------------------------------------------------
# Prompt formatting
# --------------------------------------------------------------------------

DEFAULT_MAX_CHARS_PER_CHUNK = MAX_CHUNK_CHARS


def format_untrusted_chunks(
    chunks: Sequence[RetrievedChunk],
    *,
    max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
) -> str:
    """Retrieved chunks as prompt text: the notice, then one block per chunk.

    Paths, symbols and code all come from the target repository and are
    attacker-controlled. ``untrusted_block`` escapes look-alike opening and
    closing tags in the body and strips the label to safe characters, so
    nothing a chunk contains can end its block early. The symbol is written
    inside the body, not the label, for the same reason.
    """
    if not chunks:
        return ""
    # Imported here: agents pulls in the whole LangGraph runtime, which the
    # chunker and embedders (and the indexing task) do not need.
    from .agents import UNTRUSTED_DATA_NOTICE, untrusted_block

    blocks = []
    for c in chunks:
        label = f"{c.path}#L{c.start_line}-L{c.end_line}"
        body = c.content
        if max_chars_per_chunk is not None and len(body) > max_chars_per_chunk:
            body = body[:max_chars_per_chunk] + "\n...[truncated]"
        if c.symbol:
            body = f"symbol: {c.symbol}\n{body}"
        blocks.append(untrusted_block(label, body))
    return UNTRUSTED_DATA_NOTICE + "\n\n" + "\n\n".join(blocks)
