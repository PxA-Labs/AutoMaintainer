# Retrieval over the target repository (RAG)

Issue [#526](https://github.com/PxA-Labs/AutoMaintainer/issues/526). This round
adds the storage, indexing and retrieval pieces. **Nothing is wired into the
agents' prompts yet**: round 18 does that behind the `Retriever` protocol.

| Piece | Where |
| --- | --- |
| Table, index, RLS, search function | `supabase/migrations/20261118120000_code_chunks_pgvector.sql` |
| Chunker, embedders, retrievers, prompt formatting | `backend/automaintainer/_runtime/retrieval.py` |
| Indexing task (Celery, `maintenance` queue) | `backend/rag_tasks.py` |
| Offline recall eval | `backend/evals/rag_recall.py` |
| Tests | `backend/tests/rag/`, `backend/tests/rls/test_code_chunks_rls.py`, `backend/tests/redteam/test_retrieved_text_injection.py`, `backend/evals/tests/test_rag_recall.py` |

## How it works

1. **Chunking.** `chunk_repository(path)` walks a checkout (skipping `.git`,
   `node_modules`, virtualenvs, build output, symbolic links, lock files,
   minified files, binary and non-UTF-8 files, and files over 256 kB). Files
   named like credentials are never chunked, so they are neither sent to
   Vertex nor readable in `code_chunks`: `.env*`, `*.pem` and `*.key` (the
   secret entries of `change_limits.DEFAULT_DENYLIST`), SSH private keys
   (`id_rsa*`, `id_ed25519*` ...), keystores (`*.p12`, `*.pfx`, `*.jks`) and
   `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`. This is a name list, not
   secret scanning: a token committed inside ordinary source is still
   indexed. Python,
   JavaScript and TypeScript files are split with the existing tree-sitter
   indexer (`ast_indexer`, read-only): one chunk per top-level function or
   method (`Class.method`, `Outer.Inner.method` for nested classes; nested
   functions and classes defined inside a function stay in their parent) and
   one per class *header* (docstring and attributes, without the methods, so
   nothing is embedded twice). Python decorators belong to what they decorate,
   including multi-line ones and comments between a decorator and its `def`:
   the span is tree-sitter's `decorated_definition`. Code outside every
   definition (imports, module constants and docstrings, `if __name__` blocks,
   class attributes between methods) becomes *gap* chunks: symbol `None` at
   module level, the class's name inside a class; runs with no word character
   (a lone `}`) are skipped. Other text files, and source files with no
   definitions, become file-level chunks. Chunks over 150 lines are split
   into windows; each window is cut at 6000 characters.
2. **Embedding.** `VertexEmbedder` calls Vertex AI `text-embedding-005`
   (768 dimensions) through `google-genai`, the optional `rag` extra
   (`uv sync --extra rag`). Documents use task type `RETRIEVAL_DOCUMENT`,
   queries `CODE_RETRIEVAL_QUERY`. Requests are batched
   (`RAG_EMBEDDING_BATCH_SIZE`, default 16) and transient failures (429, 5xx,
   connection errors) are retried with exponential backoff (1 s, 2 s, 4 s ...,
   at most 30 s, 5 retries). What is embedded is the path, the symbol and the
   code; `content_sha` is the sha256 of exactly that text.
3. **Storage.** `code_chunks` holds one row per chunk and commit, with an
   HNSW index (`vector_cosine_ops`). The `vector` extension lives in the
   `extensions` schema, as Supabase recommends.
4. **Search.** `match_code_chunks(org, repository, query_embedding, k)`
   returns path, symbol, lines, content and similarity (1 - cosine distance)
   of the k nearest chunks of one repository; k is clamped to 0..50.
   `PgvectorRetriever` calls it over PostgREST; `InMemoryRetriever` ranks in
   Python (tests and the offline eval).
5. **Prompt formatting.** `format_untrusted_chunks(chunks)` returns
   `UNTRUSTED_DATA_NOTICE` followed by one `untrusted_block` per chunk, so
   retrieved code is data, like every other piece of repository text.

### Tenancy

- RLS is enabled on `code_chunks`. Org members can `SELECT` their own
  organization's rows (`current_user_org_ids()`, as on every org table).
  Clients have no `INSERT`/`UPDATE`/`DELETE` policy; only the service role
  writes.
- `match_code_chunks` is `SECURITY INVOKER` with an empty `search_path`
  (every object in it is schema-qualified): a
  member calling it is limited by RLS, and the backend (service role, which
  bypasses RLS) is limited by the function's own `org_id` **and**
  `repository_id` filter. `anon` cannot execute it.
- A retriever's org and repository are fixed when it is built; `retrieve()`
  takes only the query, so a model-facing tool built on it cannot ask for
  another tenant's code.
- The indexing task reads the organization from the `repositories` row; the
  caller cannot file chunks under another org.
- Chunks are deleted with their repository and their organization
  (`ON DELETE CASCADE`).

`backend/tests/rls/test_code_chunks_rls.py` checks these against a real
PostgreSQL with pgvector, including the cross-org case: org A's member asking
for org B's repository gets nothing, and the service role asking with a
mismatched org and repository gets nothing, although every tenant's chunks
have identical embeddings. The `ci-db` job runs them (and the general
`test_rls_policies.py`, which seeds `code_chunks` for every tenant) on
`pgvector/pgvector:0.8.6-pg16`, pinned by digest; the official `postgres:16`
image has no pgvector, so the migration cannot apply there. To run them
locally, point `DATABASE_URL_RLS_TEST` at any PostgreSQL 16 with pgvector.

### Indexing

`rag_tasks.index_repository_task(repository_id, commit_sha, clone_path)`
indexes one checkout that is already at `commit_sha` (it checks with
`git rev-parse HEAD`; `commit_sha` must be the full 40-character sha, in
either case, and is stored lower-case). It does nothing unless `RAG_INDEXING_ENABLED` is `1`,
`true`, `yes` or `on`.

- **Incremental.** A chunk whose `content_sha` is already stored for the
  repository (any commit) reuses that embedding: a new commit pays only for
  what changed.
- **Idempotent.** Rows are upserted on the unique key
  `(repository_id, commit_sha, path, start_line, content_sha)` with
  ignore-duplicates; running the same commit again embeds and writes nothing.
- **One commit per repository.** After writing, the repository's rows of
  every other commit are deleted. New rows are written first, so a query in
  between can see both commits, never neither.
- **One run per repository at a time.** Because each run deletes the other
  commits' rows, two concurrent runs could empty the index. The task holds a
  Redis lock (`automaintainer:rag-index-lock:<repository_id>`, 1 h TTL,
  released by compare-and-delete) and retries every 60 s (up to 30 times)
  while another run holds it. It is separate from the agent-run lock, so
  indexing and agent runs never block each other.
- **Cost guard.** A repository with more than `RAG_MAX_CHUNKS` (default
  20000) chunks is skipped, not partly indexed. The variable is read when
  indexing; a value that is not a positive integer falls back to the default
  with a warning (workers import the module, so it must not fail at import).
- Nothing enqueues the task yet; round 18 does (e.g. when a PR merges).

Known limits: commit order is not checked (indexing an older commit after a
newer one leaves the older one until the next run). A chunk's `org_id` is
copied from the repository when it is indexed: if a repository row moves to
another organization, its chunks stay readable by the **old** organization's
members until the repository is indexed again. Any index run (the same
commit or a new one) first deletes the repository's rows filed under
another organization, reusing their embeddings, and writes them again under
the current one. Between the transfer and that run the exposure remains, so
whoever transfers a repository between organizations should delete its
`code_chunks` rows (service role) in the same change, or enqueue an index
run; round 18 can enforce this with a trigger on `repositories.org_id`. HNSW is searched before
the org/repository filter is applied, so with many repositories in one table
fewer than k rows can come back (pgvector's `hnsw.ef_search`, 40 by default,
bounds the candidates; raising it, or pgvector 0.8's iterative scans, are the
fixes if this shows up).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAG_INDEXING_ENABLED` | unset (off) | Turns the indexing task on. |
| `GOOGLE_CLOUD_PROJECT` | none (required for Vertex) | Project billed for embeddings. |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Vertex AI region the code is sent to. |
| `RAG_EMBEDDING_MODEL` | `text-embedding-005` | Must return 768 dimensions (the column width). |
| `RAG_EMBEDDING_BATCH_SIZE` | `16` | Inputs per Vertex request (1..250). |
| `RAG_MAX_CHUNKS` | `20000` | Per-repository cost guard. |

Credentials are application default credentials; on Cloud Run, the service
account needs `roles/aiplatform.user`.

## Data egress

With the Vertex embedder enabled, **the target repository's code leaves the
deployment**: every chunk (its path, symbol name and source text) is sent to
Vertex AI in `GOOGLE_CLOUD_LOCATION` of `GOOGLE_CLOUD_PROJECT`, and at query
time so is the query (issue text). The chunks are also stored in Supabase
(`code_chunks.content`), next to the embeddings. Before turning indexing on
for a repository, the owner must be allowed to send that code to Google
Cloud in that region; Google's terms for Vertex AI (including whether
prompts and inputs may be used for training) are the ones to check, in the
project's own agreement. The `HashingEmbedder` sends nothing anywhere, but it
is a lexical baseline, not a semantic model.

## Cost

Embedding cost is roughly (characters embedded) x (Vertex AI's price per
character for the model). Check the current price on the Vertex AI
generative AI pricing page; as last seen by the authors it was in the order
of USD 0.000025 per 1,000 input characters for online text embeddings,
which would make a 20,000-chunk repository at ~1,500 characters per chunk
(30 M characters) cost about USD 0.75 to index once. Re-indexing a commit
costs only the changed chunks; queries are a few hundred characters each.
Treat these figures as an estimate to verify, not a quote. Storage: 768
floats are ~3 kB per row plus the HNSW index, in the Supabase database.

## Evaluation

`python -m evals.rag_recall` (from `backend/`) measures offline recall@k on
the 24 end-to-end tasks (`evals/e2e/tasks`). The query is the issue text, the
corpus is the task's `repo/` snapshot, and the gold is derived from the
oracle fix: the gold file is `target_file`, the gold chunks are its chunks
overlapping the lines `solution/` changes. It also prints what drawing k
chunks at random would score on the same snapshot.

Results with the `HashingEmbedder` (deterministic, offline; this round, with
gap chunks for module-level code):

| k | file recall@k | random | function recall@k | random |
| --- | --- | --- | --- | --- |
| 1 | 0.75 | 0.81 | 0.42 | 0.04 |
| 3 | 0.96 | 0.99 | 0.62 | 0.13 |
| 5 | 1.00 | 1.00 | 0.79 | 0.22 |
| 10 | 1.00 | 1.00 | 0.92 | 0.40 |

Before gap chunks (definitions only), function recall was 0.42 / 0.75 / 0.79
/ 0.96 and file recall@1 0.83. The gold here is always a function, so the
benchmark only sees the cost of the extra chunks (import blocks share many
identifiers with issue texts), not the gain on questions about module-level
code, which none of the 24 tasks asks.

How to read them:

- **File recall says nothing here.** Each snapshot is the target file plus a
  visible test file, a package marker and a license, so random chunks hit the
  gold file almost always.
- **Function recall@5 = 0.79 (19 of 24)** against 0.22 by chance. Many issue
  texts name the function or its file, which a bag-of-identifiers embedder
  matches easily; this is an easy, small benchmark (7 to 128 chunks per
  snapshot), not evidence about real repositories. Misses at k=5:
  `cachetools_ttl_boundary`, `isoweek_day_jan4`, `jsonpointer_leading_zero`,
  `jsonpointer_unescape_order`, `xmltodict_repeated_elements`.
- These are not Vertex numbers. **Real recall needs the Vertex embedder**, an
  owner action because it sends the snapshots' code to Google and costs money
  (the 24 snapshots are small; a few cents at the price above):

  ```bash
  cd backend
  uv sync --frozen --extra server --extra rag --group dev
  gcloud auth application-default login     # or a service account
  GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=us-central1 \
      uv run --frozen python -m evals.rag_recall --embedder vertex --k 5 \
      --out rag_recall_vertex.json
  ```

  Report the Vertex numbers next to the hashing baseline above. A
  hand-labelled query set on a real repository and the end-to-end effect
  (file localization, merged PRs, cost per run) are still open in #526.

## Owner actions before this is used

- Apply the migration (`supabase db push` or `python scripts/db-migrate up`).
- Build the worker image with the `rag` extra as well
  (`uv sync --frozen --extra server --extra rag`). The images install
  `--extra server` only, so `google-genai` is missing there and the first
  embedding call would fail with `ImportError` once indexing is enabled.
- Grant the runtime service account `roles/aiplatform.user`, set
  `GOOGLE_CLOUD_PROJECT`, then `RAG_INDEXING_ENABLED=true`.
- Confirm the Vertex embedding price on Google's pricing page; the figure in
  [Cost](#cost) is from memory.
