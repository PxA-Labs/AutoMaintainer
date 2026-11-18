-- ============================================================================
-- Code chunks follow their repository's organization (#526)
-- ============================================================================
--
-- code_chunks.org_id is a copy of repositories.org_id taken when a chunk is
-- indexed, and RLS reads it. A repository never changes organization in
-- normal operation (repositories_org_immutable rejects the UPDATE; a move
-- deletes the row, and ON DELETE CASCADE removes its chunks). But if an
-- operator moves one deliberately (disabling that trigger), or the indexer
-- read the organization just before such a move, the copy would go stale and
-- the old organization's members would keep reading the code.
--
-- This makes the copy impossible to get wrong: a composite foreign key from
-- code_chunks (repository_id, org_id) to repositories (id, org_id) with
--   ON UPDATE CASCADE  - when a repository row moves to another org, its
--                        chunks move with it in the same statement (they
--                        are the repository's code, now owned by the new
--                        org; the old org can no longer read them);
--   ON DELETE CASCADE  - as before, chunks go with the repository.
-- PostgreSQL implements both with its internal referential-integrity
-- triggers, so no trigger function of our own is needed. An INSERT whose
-- org_id is not its repository's org now fails, which closes the indexer's
-- read-then-write race as well.
--
-- Existing rows whose org_id already disagrees with their repository (left
-- by a move before this migration) are deleted first: the next index run
-- writes them again under the right org. repositories gets a UNIQUE
-- (id, org_id) so it can be referenced; id alone is already its primary key,
-- so this adds an index but changes no rule.
--
-- Re-runnable: the DELETE matches nothing the second time, and each
-- constraint is added only when pg_constraint does not have it yet.

DELETE FROM code_chunks c
USING repositories r
WHERE c.repository_id = r.id
  AND c.org_id IS DISTINCT FROM r.org_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'repositories_id_org_key'
          AND conrelid = 'public.repositories'::regclass
    ) THEN
        ALTER TABLE repositories ADD CONSTRAINT repositories_id_org_key
            UNIQUE (id, org_id);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'code_chunks_repository_org_fkey'
          AND conrelid = 'public.code_chunks'::regclass
    ) THEN
        ALTER TABLE code_chunks ADD CONSTRAINT code_chunks_repository_org_fkey
            FOREIGN KEY (repository_id, org_id)
            REFERENCES repositories (id, org_id)
            ON UPDATE CASCADE ON DELETE CASCADE;
    END IF;
END;
$$;
