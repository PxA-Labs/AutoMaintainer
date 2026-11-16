-- ============================================================================
-- Per-repository merge policy (epic #411)
-- ============================================================================
--
-- repositories.merge_policy: a repository's own merge policy, or NULL to
-- inherit the deployment's MERGE_POLICY. MERGE_POLICY is the ceiling: the
-- backend uses the stricter of the two (human < ci < auto, see
-- backend/merge_policy.py), so this column can only ever make a repository
-- merge less readily than the operator allowed.
--
-- Written by the backend only (service role), through
-- PUT /repositories/{id}/merge-policy, which allows org owners and admins
-- and refuses a value looser than MERGE_POLICY. Org members read it with the
-- rest of the row (repos_select). repositories has no INSERT/UPDATE/DELETE
-- policy for anon or authenticated, so RLS already rejects their writes; the
-- trigger below also rejects them should such a policy ever be added for
-- other columns.
--
-- Re-runnable (IF NOT EXISTS, a pg_constraint check before ADD CONSTRAINT,
-- CREATE OR REPLACE, DROP TRIGGER IF EXISTS before CREATE).

ALTER TABLE repositories ADD COLUMN IF NOT EXISTS merge_policy TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'repositories_merge_policy_check'
          AND conrelid = 'public.repositories'::regclass
    ) THEN
        ALTER TABLE repositories ADD CONSTRAINT repositories_merge_policy_check
            CHECK (merge_policy IS NULL OR merge_policy IN ('human', 'ci', 'auto'));
    END IF;
END;
$$;

COMMENT ON COLUMN repositories.merge_policy IS
    'Repository merge policy (human, ci, auto); NULL inherits MERGE_POLICY. '
    'Effective policy = the stricter of this and MERGE_POLICY. Service role writes only.';

-- current_user is the role PostgREST switched to (anon or authenticated for
-- client requests, service_role for the backend); migrations and the SQL
-- editor run as other roles and are allowed.
CREATE OR REPLACE FUNCTION repositories_merge_policy_service_only()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF current_user IN ('anon', 'authenticated') AND (
        (TG_OP = 'INSERT' AND NEW.merge_policy IS NOT NULL)
        OR (TG_OP = 'UPDATE' AND NEW.merge_policy IS DISTINCT FROM OLD.merge_policy)
    ) THEN
        RAISE EXCEPTION 'repositories.merge_policy is written by the backend only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS repositories_merge_policy_service_only ON repositories;
CREATE TRIGGER repositories_merge_policy_service_only
    BEFORE INSERT OR UPDATE OF merge_policy ON repositories
    FOR EACH ROW EXECUTE FUNCTION repositories_merge_policy_service_only();
