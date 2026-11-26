# Code retrieval (RAG)

Code retrieval indexes the default branch of each target repository in a
pgvector table and gives the agents the code chunks nearest to the task: the
PM sees them in its prompt, and the ADK Reviewer can search them with its
`search_code` tool. Tracking issue: [#526](https://github.com/PxA-Labs/AutoMaintainer/issues/526).

**Off by default.** Indexing and retrieval each have their own switch, and
both are off unless configured ([Configuration](#configuration)). With
retrieval off, the PM prompt and the Reviewer's behaviour are byte-for-byte
what they were before retrieval existed. On Google Cloud, one Terraform
switch, `rag_enabled` (default `false`), turns both on
([Owner steps](#owner-steps)).

**Data leaves the deployment when it is on.** The Vertex AI embedder sends
repository code to Google Cloud. Read [Data egress](#data-egress) before
enabling it.

| Piece | Where |
| --- | --- |
| Table, index, RLS, search function | `supabase/migrations/20261118120000_code_chunks_pgvector.sql` |
| Chunks follow their repository's org | `supabase/migrations/20261118183000_code_chunks_org_transfer.sql` |
| Chunker, embedders, retrievers, prompt formatting | `backend/automaintainer/_runtime/retrieval.py` |
| PM prompt wiring, retriever resolution | `backend/automaintainer/_runtime/agents.py` (`retrieved_context`, `resolve_retriever`) |
| Engine injection | `backend/automaintainer/engine.py`, `backend/automaintainer/context.py` |
| Reviewer `search_code` over the index | `backend/automaintainer/_specialists/tools.py`, `reviewer_adk.py` |
| Indexing task and its trigger (Celery, `maintenance` queue) | `backend/rag_tasks.py` |
| Terraform switch | `infra/terraform/variables.tf` (`rag_enabled`, `rag_location`), `main.tf`, `iam.tf` |
| Offline recall evaluation | `backend/evals/rag_recall.py` |
| Tests | `backend/tests/rag/`, `backend/tests/rls/test_code_chunks_rls.py`, `backend/tests/redteam/test_retrieved_text_injection.py`, `backend/tests/specialists/test_search_code_*.py`, `backend/tests/concurrency/test_concurrency_rag_retriever.py`, `backend/evals/tests/test_rag_recall.py` |

The embedders depend on `google-genai`, which comes with the optional `rag`
extra (`uv sync --extra rag`). The `Test with the adk and rag extras` job in
`.github/workflows/ci-backend.yml` installs that extra and fails if any of
the `tests/rag` tests skip.

## What is indexed and how

### Chunking

`chunk_repository(path)` walks a checkout and turns each text file into
chunks.

It skips:

- directories: `.git`, `node_modules`, virtual environments, build output and
  tool caches;
- symbolic links, lock files (`package-lock.json`, `uv.lock`, `*.lock` and
  others), minified files and source maps;
- binary and non-UTF-8 files, and files over 256 kB;
- files named like credentials: `.env*`, `*.pem` and `*.key` (the secret
  entries of `change_limits.DEFAULT_DENYLIST`), SSH private keys
  (`id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`), keystores (`*.p12`,
  `*.pfx`, `*.jks`, `*.keystore`), `.npmrc`, `.pypirc`, `.netrc` and
  `.git-credentials`. These are never sent to Vertex AI or stored in
  `code_chunks`. This is a name list, not secret scanning: a token committed
  inside ordinary source is still indexed.

How files are split:

- **Python, JavaScript and TypeScript** go through the existing tree-sitter
  indexer (`ast_indexer`, read-only). Each top-level function or method is
  one chunk (`Class.method`, or `Outer.Inner.method` for nested classes);
  functions and classes defined inside a function stay in their parent. Each
  class header (docstring and attributes, without the methods) is one chunk,
  so nothing is embedded twice. Python decorators belong to what they
  decorate, including multi-line decorators and comments between a decorator
  and its `def` (tree-sitter's `decorated_definition` span).
- **Code outside every definition** (imports, module constants and
  docstrings, `if __name__` blocks, class attributes between methods) becomes
  gap chunks: symbol `None` at module level, or the class's name inside a
  class. Runs with no word character, such as a lone `}`, are skipped.
- **Other text files**, and source files without definitions, become
  file-level chunks.
- Chunks over 150 lines are split into windows, and each window is cut at
  6000 characters.

### Embedding

`VertexEmbedder` calls Vertex AI `text-embedding-005` (768 dimensions)
through `google-genai`. Documents use task type `RETRIEVAL_DOCUMENT` and
queries `CODE_RETRIEVAL_QUERY`. Requests are batched
(`RAG_EMBEDDING_BATCH_SIZE`, default 16, at most 250). Transient failures
(HTTP 408, 429, 500, 502, 503, 504 and connection errors) are retried up to 5
times with exponential backoff (1 s, 2 s, 4 s and so on, capped at 30 s).
The embedded text is the path, the symbol and the code; `content_sha` is the
SHA-256 of exactly that text.

`HashingEmbedder` is a deterministic, offline, lexical baseline that also
produces 768-dimensional vectors. Tests and the offline evaluation use it.

### Storage and search

- `code_chunks` holds one row per chunk and commit, with an HNSW index
  (`vector_cosine_ops`) on the `vector(768)` column. The `vector` extension
  lives in the `extensions` schema, as Supabase recommends.
- `match_code_chunks(org, repository, query_embedding, k)` returns the path,
  symbol, lines, content and similarity (1 minus cosine distance) of the k
  nearest chunks of one repository. k is clamped to 0..50.
- `PgvectorRetriever` calls the function over PostgREST. `InMemoryRetriever`
  ranks in Python and serves tests and the offline evaluation.
- `format_untrusted_chunks(chunks)` returns `UNTRUSTED_DATA_NOTICE`
  followed by one `untrusted_block` per chunk. Retrieved code is data, like
  every other piece of repository text.

## Schema and row-level security

- RLS is enabled on `code_chunks`. Org members can `SELECT` their own
  organization's rows (`current_user_org_ids()`, as on every org table).
  Clients have no `INSERT`, `UPDATE` or `DELETE` policy; only the service
  role writes.
- `match_code_chunks` is `SECURITY INVOKER` with an empty `search_path`, and
  every object in it is schema-qualified. A member calling it is limited by
  RLS. The backend's service role bypasses RLS and is limited by the
  function's own `org_id` and `repository_id` filter. `anon` cannot execute
  it.
- A retriever's org and repository are fixed when it is built. `retrieve()`
  takes only the query, so a model-facing tool built on it cannot ask for
  another tenant's code.
- The indexing task reads the organization from the `repositories` row; the
  caller cannot file chunks under another org.
- Chunks are deleted with their repository and their organization
  (`ON DELETE CASCADE`).

**Organization transfer.** A chunk's `org_id` is always its repository's. A
composite foreign key `(repository_id, org_id) -> repositories (id, org_id)`
with `ON UPDATE CASCADE`:

- moves the chunks in the same statement when a repository row moves to
  another org (which `repositories_org_immutable` allows only to an operator
  who disables it), so the old org cannot read them afterwards;
- refuses an insert filed under another org, for example by an indexer that
  read the org just before a move.

The migration deletes rows that already disagreed; the next index run writes
them again.

`backend/tests/rls/test_code_chunks_rls.py` checks these rules against a real
PostgreSQL with pgvector, including the cross-org case: org A's member asking
for org B's repository gets nothing, and the service role asking with a
mismatched org and repository gets nothing, although every tenant's chunks
have identical embeddings. The `ci-db` workflow runs them, and the general
`test_rls_policies.py`, on `pgvector/pgvector:0.8.6-pg16`, pinned by digest.
The official `postgres:16` image has no pgvector, so the migration cannot
apply there. To run the tests locally, point `DATABASE_URL_RLS_TEST` at any
PostgreSQL 16 with pgvector.

## Indexing

### The indexing task

`rag_tasks.index_repository_task(repository_id, commit_sha, clone_path)`
indexes one checkout that is already at `commit_sha`. It checks the checkout
with `git rev-parse HEAD`; `commit_sha` must be the full 40-character SHA, in
either case, and is stored lower-case. The task does nothing unless
`RAG_INDEXING_ENABLED` is `1`, `true`, `yes` or `on`.

- **Incremental.** A chunk whose `content_sha` is already stored for the
  repository, at any commit, reuses that embedding. A new commit pays only
  for what changed.
- **Idempotent.** Rows are upserted on the unique key
  `(repository_id, commit_sha, path, start_line, content_sha)` with
  ignore-duplicates. Running the same commit again embeds and writes nothing.
- **One commit per repository.** After writing, the repository's rows of
  every other commit are deleted. New rows are written first, so a query in
  between can see both commits, never neither.
- **One run per repository at a time.** Because each run deletes the other
  commits' rows, two concurrent runs could empty the index. The task holds a
  Redis lock (`automaintainer:rag-index-lock:<repository_id>`, 1 h TTL,
  released by compare-and-delete) and retries every 60 s, up to 30 times,
  while another run holds it. This lock is separate from the agent-run lock,
  so indexing and agent runs never block each other.
- **Cost guard.** A repository with more than `RAG_MAX_CHUNKS` chunks
  (default 20000) is skipped, not partly indexed. The variable is read at
  indexing time; a value that is not a positive integer falls back to the
  default with a warning.

### The trigger

Every worker loads `rag_tasks` (it is in `celery_app`'s include list), which
connects a Celery `task_prerun` handler:

1. When an agent run starts (`tasks.run_agent_loop_task`) and
   `RAG_INDEXING_ENABLED` is on, the handler enqueues
   `ensure_repository_indexed_task(repository_id)` on the `maintenance`
   queue, at most once per repository every 10 minutes (a Redis key; a failed
   enqueue releases it). Failures are logged by type and never reach the run.
2. The task reads the repository row and asks `git ls-remote` for the default
   branch head, with the installation's token passed through git's
   environment. It returns `fresh` when the index already holds that commit
   under the repository's org.
3. Otherwise it shallow-clones the branch into a throwaway directory, indexes
   the commit actually cloned under the lock above (returning `busy` instead
   of retrying when another run holds it) and deletes the clone.

The run's own clone is not used: maintenance workers do not share the agent
workers' workspace, and later runs update that clone while indexing would
read it. The run that triggered indexing does not wait for it; its prompts
use what is already indexed, or nothing.

## Retrieval in the agents

Retrieval is off unless `rag_retrieval_enabled` (an engine's config) or, for
code running without an engine, `RAG_RETRIEVAL_ENABLED` is on. It applies only
to runs that carry a repository id; every run queued through the API or a
webhook does.

### Resolving the retriever

`agents.resolve_retriever(repository_id, commit_sha=None)` runs per prompt
and is never cached in a module global. It returns, in order of preference:

1. **An engine's injected factory.** `AutoMaintainerEngine(config, ...,
   retriever=factory)` takes a factory, like `llm=` and `github=`, and
   publishes it as `EngineContext.retriever` (default `None`). The factory is
   called with a `RetrievalScope(repository_id, commit_sha)` and returns a
   `Retriever` or `None`. It is consulted only when the engine's config has
   `rag_retrieval_enabled=True`. Two engines in one process each see only
   their own factory (`tests/concurrency/test_concurrency_rag_retriever.py`).
2. **The default.** A `PgvectorRetriever` over `get_supabase()` (inside an
   engine, the engine's own client; an engine without a database gets none)
   with the `VertexEmbedder`. The org is read from the `repositories` row,
   never taken from the run. With a `commit_sha`, the retriever is returned
   only when the index holds exactly that commit.

### PM prompt

`pm_node` queries with the issue text (title and body in targeted mode,
otherwise the proposed idea), capped at 2000 characters, and asks for
`rag_top_k` chunks. They are appended after the directive and idea blocks,
under their own header, formatted by `format_untrusted_chunks` and capped at
12000 characters in total. The best chunks come first; the first chunk that
does not fit is cut and the rest are dropped. The system prompt is unchanged.

Building the retriever, embedding the query and the RPC run in a thread under
a 10-second timeout. Any error, or the timeout, is logged by exception type,
and the PM gets exactly the prompt it would get with retrieval off. A run
resumed after the PM decided does not query again.
`tests/rag/test_pm_retrieval.py` pins this behaviour, including that the
prompt with the flag off equals the pre-retrieval prompt.

### ADK Reviewer

This applies only when `reviewer_backend` is `adk`; the default `langgraph`
review does not use retrieval. `review_pull_request` asks for a retriever
scoped to the run's repository and the PR head commit. `search_code` then
returns the nearest chunks (path, lines, symbol and numbered code), with k
bounded by the tool's result limit and output bounded by its character cap.

In every other case the tool uses ripgrep exactly as before: retrieval off,
no repository id, an index at another commit, a failure resolving or
querying the index, or an empty result. (`ReviewTools.search_code` also
skips the index for `regex=True`, but the tool the model calls,
`search_code(query)`, never passes it.) Only default branches
are indexed, so a PR head is rarely indexed and the Reviewer mostly keeps
using ripgrep. Serving the base branch's chunks for a head review was
rejected as misleading. Arguments are validated before either path, a call
costs one of the review's tool calls, its result reaches the model in an
untrusted-data block, and the budgets are unchanged.

Resolving the retriever runs under the same 10-second timeout as the PM's
query (`RAG_RETRIEVAL_TIMEOUT_SECONDS`). Each `search_code` query against
the index has no timeout of its own: only the review's 180-second deadline
bounds it. A slow index therefore costs review time rather than failing
over to ripgrep.

### Red-team coverage

The prompt-injection fixtures of epic #411 run with retrieval on and an index
whose code carries the payload, forged closing tags included:

- through the PM prompt: `tests/redteam/test_retrieved_text_injection.py`;
- through the ADK Reviewer's `search_code`:
  `tests/specialists/test_search_code_redteam.py`.

The payload stays inside untrusted-data blocks. Every outcome (held for an
untrusted trigger, rejected by a well-behaved PM, merged for a writer) and
every `merge_decisions` row is the same as without retrieval.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAG_INDEXING_ENABLED` | unset (off) | Turns on the indexing task and its run-start trigger. Accepts `1`, `true`, `yes`, `on`. |
| `RAG_RETRIEVAL_ENABLED` | unset (off) | Retrieved code in the PM prompt and the ADK Reviewer's `search_code`. Engine field: `rag_retrieval_enabled`. Accepts `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off`; `AutoMaintainerConfig.from_env` refuses anything else, and the runtime logs it and stays off. |
| `RAG_TOP_K` | `8` | Chunks per query, 1 to 50 (the RPC's bound). Engine field: `rag_top_k`. |
| `GOOGLE_CLOUD_PROJECT` | none | Project billed for embeddings. Required for Vertex AI. |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Vertex AI region the code is sent to. |
| `RAG_EMBEDDING_MODEL` | `text-embedding-005` | Must return 768 dimensions (the column width). |
| `RAG_EMBEDDING_BATCH_SIZE` | `16` | Inputs per Vertex AI request. Values outside 1 to 250 are clamped to that range; a value that is not an integer raises `ValueError` when the embedder is built, which ends an indexing task and turns retrieval off for that query (logged). |
| `RAG_MAX_CHUNKS` | `20000` | Per-repository cost guard for indexing. |

Credentials are application default credentials. The service account that
embeds needs `roles/aiplatform.user`.

## Data egress

With the Vertex AI embedder in use, the target repository's code leaves the
deployment:

- Every chunk (its path, symbol name and source text) is sent to Vertex AI in
  `GOOGLE_CLOUD_LOCATION` of `GOOGLE_CLOUD_PROJECT` when it is indexed.
- At query time, the PM's query (issue text, capped at 2000 characters) is
  sent from the agent workers when `RAG_RETRIEVAL_ENABLED` is on.
- With the ADK Reviewer, each `search_code` query the model writes (at most
  200 characters) is also sent, when the index holds the PR head commit.
  The model writes that query after reading the diff, so it can contain
  text from the change under review.
- The chunks are also stored in Supabase (`code_chunks.content`), next to the
  embeddings, readable by members of the repository's organization.

Before turning indexing on for a repository, the owner must be allowed to
send that code to Google Cloud in that region. Check Google's terms for
Vertex AI in the project's own agreement, including whether inputs may be
used for training. The `HashingEmbedder` sends nothing anywhere, but it is a
lexical baseline, not a semantic model.

## Cost

Embedding cost is approximately the number of characters embedded multiplied
by Vertex AI's price per character for the model. This document does not
quote a price; check the current Vertex AI pricing page before enabling
indexing.

Bounds that follow from the code:

- A chunk is at most 6000 characters of code plus its path and symbol, and
  `RAG_MAX_CHUNKS` (default 20000) caps the chunks per repository, so a full
  index of one repository embeds at most about 120 million characters at the
  defaults. Typical chunks are much shorter than the cap.
- Re-indexing a commit embeds only chunks whose `content_sha` is new.
- Each PM query embeds at most 2000 characters. The Reviewer embeds a query
  only when the index holds the PR head commit.

Storage: each row holds 768 floats (about 3 kB) plus the HNSW index, in the
Supabase database.

## Offline evaluation

`python -m evals.rag_recall` (from `backend/`) measures offline recall@k on
the 24 end-to-end tasks in `evals/e2e/tasks` ([EVALS.md](EVALS.md#task-set)).
For each task:

- the query is the issue text;
- the corpus is the task's `repo/` snapshot;
- the gold file is `target_file`, and the gold chunks are its chunks that
  overlap the lines `solution/` changes;
- the baseline is the recall of drawing k chunks at random from the same
  snapshot.

Options: `--k` (default 5), `--embedder hashing|vertex` (default
`hashing`), `--tasks` (comma-separated task ids, or `all`, the default),
`--tasks-dir` (default: the Implementer task set in `backend/evals/tasks`)
and `--out FILE` (write the JSON report).

Results with the `HashingEmbedder`, reproduced by running the command at each
k:

| k | File recall@k | Random | Function recall@k | Random |
| --- | --- | --- | --- | --- |
| 1 | 0.75 | 0.81 | 0.42 | 0.04 |
| 3 | 0.96 | 0.99 | 0.62 | 0.13 |
| 5 | 1.00 | 1.00 | 0.79 | 0.22 |
| 10 | 1.00 | 1.00 | 0.92 | 0.40 |

How to read them:

- **File recall carries no information here.** Each snapshot is the target
  file plus a visible test file, a package marker and a license, so random
  chunks almost always hit the gold file.
- **Function recall@5 is 0.79 (19 of 24), against 0.22 by chance.** Many
  issue texts name the function or its file, which a bag-of-identifiers
  embedder matches easily. The benchmark is small (7 to 128 chunks per
  snapshot) and easy; it is not evidence about real repositories. The misses
  at k=5 are `cachetools_ttl_boundary`, `isoweek_day_jan4`,
  `jsonpointer_leading_zero`, `jsonpointer_unescape_order` and
  `xmltodict_repeated_elements`.
- **The gold is always a function,** so the benchmark sees the cost of gap
  chunks (import blocks share many identifiers with issue texts) but not
  their benefit on questions about module-level code.
- **These are not Vertex AI numbers.** The tests in
  `evals/tests/test_rag_recall.py` check how recall is computed, not these
  values.

The end-to-end evaluation does not enable retrieval, so its baseline is a
retrieval-off baseline. Whether a model decides better with the retrieved
context needs a live run. A hand-labelled query set on a real repository and
the end-to-end effect (file localization, merged PRs, cost per run) are still
open in #526.

## Owner steps

These steps need credentials or cost money; the code does not perform them.

1. **Measure Vertex AI recall** before enabling indexing or retrieval. This
   sends the 24 snapshots' code to Google and is billed:

   ```bash
   cd backend
   uv sync --frozen --extra server --extra rag --group dev
   gcloud auth application-default login     # or a service account
   GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=us-central1 \
       uv run --frozen python -m evals.rag_recall --embedder vertex --k 5 \
       --out rag_recall_vertex.json
   ```

   Report the Vertex AI numbers next to the hashing baseline above.
2. **Apply the migrations** (`supabase db push` or
   `uv run --project backend --extra server python scripts/db-migrate up`), including both `code_chunks` migrations.
   See [DEPLOYMENT.md, Database](DEPLOYMENT.md#database).
3. **Build the worker image with the `rag` extra.** The `Dockerfile`'s
   `EXTRAS` build argument chooses the extras (default `server`), for example
   `docker build --build-arg EXTRAS=server,rag .`. Without the extra,
   `google-genai` is missing and the first embedding call fails with
   `ImportError` once indexing is enabled.
4. **Turn it on.**
   - **On Google Cloud (`infra/terraform`):** set `rag_enabled = true` (and
     `rag_location` if not `us-central1`), apply, set the repository variable
     `IMAGE_EXTRAS` to `server,rag`, deploy and reset the worker VM. The
     switch enables the Vertex AI API, grants the worker's service account
     `roles/aiplatform.user` (the API service gets no Vertex AI role, because
     it never embeds) and sets `RAG_INDEXING_ENABLED=true`,
     `RAG_RETRIEVAL_ENABLED=true` and `GOOGLE_CLOUD_LOCATION`.
     `GOOGLE_CLOUD_PROJECT` is always set. `app_env` refuses these three
     names. The full procedure, log lines to check and how to turn it off are
     in [DEPLOYMENT.md, Code retrieval (RAG)](DEPLOYMENT.md#code-retrieval-rag).
   - **Elsewhere:** grant the runtime service account
     `roles/aiplatform.user` and set `GOOGLE_CLOUD_PROJECT`. Set
     `RAG_INDEXING_ENABLED=true` on the maintenance worker and on the agent
     workers (their run starts trigger indexing). Set
     `RAG_RETRIEVAL_ENABLED=true` on the agent workers once the index is
     populated. Every worker that embeds needs the `rag` extra and the role;
     without them every query fails, is logged, and the prompts stay as with
     retrieval off.

On Google Cloud the one switch turns indexing and retrieval on together.
Until a repository is indexed its PM prompts get no context, but each PM
query is still embedded with Vertex AI. The Reviewer checks the indexed
commit first and does not embed when it does not match.

## Known limitations

- **Commit order is not checked.** Indexing an older commit after a newer
  one leaves the older one until the next run.
- **Only default branches are indexed,** so pull request heads usually are
  not, and the ADK Reviewer mostly uses ripgrep.
- **Filtered HNSW search can return fewer than k rows.** HNSW is searched
  before the org and repository filter is applied, so with many repositories
  in one table fewer than k rows can come back. pgvector's `hnsw.ef_search`
  (40 by default) bounds the candidates; raising it, or pgvector 0.8's
  iterative scans, are the remedies if this occurs.
- **Credential filtering is by file name only** ([Chunking](#chunking)).
