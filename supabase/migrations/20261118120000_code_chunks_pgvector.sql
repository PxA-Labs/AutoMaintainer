-- ============================================================================
-- Code chunks with pgvector embeddings for retrieval (#526)
-- ============================================================================
--
-- code_chunks: one row per function, method or class (or whole small file)
-- of a repository at one commit, with its embedding. Written by the
-- rag_tasks indexing task with the service role and read through
-- match_code_chunks(). See docs/RAG.md.
--
-- Embeddings are Vertex AI text-embedding-005 vectors: 768 dimensions,
-- compared by cosine distance, hence the HNSW index with vector_cosine_ops.
--
-- The vector extension goes in the "extensions" schema, as Supabase
-- recommends, so its types, functions and operators are not exposed through
-- PostgREST from "public". Everything below names them schema-qualified.
-- (A project that already installed vector in "public" keeps it there:
-- CREATE EXTENSION IF NOT EXISTS does not move it, and the qualified names
-- below then fail loudly; move it with ALTER EXTENSION vector SET SCHEMA
-- extensions.)
--
-- Tenancy: org members read their organization's rows (the same
-- current_user_org_ids() helper as every org table); anon and authenticated
-- have no INSERT/UPDATE/DELETE policy, so only the service role writes. Chunks are copies of customer code, so they are
-- removed with their repository (ON DELETE CASCADE) and their organization.
--
-- Re-runnable (IF NOT EXISTS, DROP ... IF EXISTS before CREATE, CREATE OR
-- REPLACE).

CREATE SCHEMA IF NOT EXISTS extensions;
GRANT USAGE ON SCHEMA extensions TO anon, authenticated, service_role;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

CREATE TABLE IF NOT EXISTS code_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    path TEXT NOT NULL,                           -- repository-relative, POSIX
    symbol TEXT,                                  -- e.g. "Queue.add"; NULL for a whole file
    start_line INTEGER NOT NULL CHECK (start_line >= 1),
    end_line INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_sha TEXT NOT NULL,                    -- sha256 (hex) of path, symbol, content
    embedding extensions.vector(768) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT code_chunks_lines_check CHECK (end_line >= start_line),
    CONSTRAINT code_chunks_unique_chunk
        UNIQUE (repository_id, commit_sha, path, start_line, content_sha)
);

CREATE INDEX IF NOT EXISTS code_chunks_org_repo_idx
    ON code_chunks(org_id, repository_id, commit_sha);
-- Unchanged-content lookups when re-indexing a new commit.
CREATE INDEX IF NOT EXISTS code_chunks_repo_content_idx
    ON code_chunks(repository_id, content_sha);
CREATE INDEX IF NOT EXISTS code_chunks_embedding_hnsw_idx
    ON code_chunks USING hnsw (embedding extensions.vector_cosine_ops);

ALTER TABLE code_chunks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "code_chunks_select" ON code_chunks;
CREATE POLICY "code_chunks_select" ON code_chunks
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

-- Only the backend writes. No INSERT, UPDATE or DELETE policy exists for
-- anon or authenticated, so RLS rejects their inserts and their updates and
-- deletes match no row (the pattern of every service-written table).
GRANT ALL ON code_chunks TO service_role;

-- The k chunks of one repository nearest to p_query_embedding.
--
-- SECURITY INVOKER: an org member calling it through PostgREST sees only
-- what the RLS policy above lets them see; the backend calls it with the
-- service role, which bypasses RLS, so the function filters by org AND
-- repository itself and never returns another tenant's code. k is clamped
-- to 0..50. similarity = 1 - cosine distance.
--
-- The HNSW index is searched first and filtered after, so with many
-- repositories in one table fewer than k rows can come back (pgvector's
-- hnsw.ef_search, 40 by default, bounds the candidates); see docs/RAG.md.
CREATE OR REPLACE FUNCTION match_code_chunks(
    p_org_id UUID,
    p_repository_id BIGINT,
    p_query_embedding extensions.vector(768),
    p_k INTEGER
)
RETURNS TABLE (
    path TEXT,
    symbol TEXT,
    start_line INTEGER,
    end_line INTEGER,
    content TEXT,
    similarity DOUBLE PRECISION
)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = public, extensions AS $$
    SELECT c.path, c.symbol, c.start_line, c.end_line, c.content,
           1 - (c.embedding OPERATOR(extensions.<=>) p_query_embedding)
    FROM public.code_chunks c
    WHERE c.org_id = p_org_id
      AND c.repository_id = p_repository_id
    ORDER BY c.embedding OPERATOR(extensions.<=>) p_query_embedding
    LIMIT LEAST(GREATEST(COALESCE(p_k, 0), 0), 50)
$$;

REVOKE ALL ON FUNCTION match_code_chunks(UUID, BIGINT, extensions.vector, INTEGER)
    FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION match_code_chunks(UUID, BIGINT, extensions.vector, INTEGER)
    TO authenticated, service_role;
