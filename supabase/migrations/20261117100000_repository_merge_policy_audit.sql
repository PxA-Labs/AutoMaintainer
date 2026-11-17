-- ============================================================================
-- Repository merge policy change audit (epic #411)
-- ============================================================================
--
-- repository_merge_policy_changes: one row per change of
-- repositories.merge_policy made through PUT /repositories/{id}/merge-policy:
-- who changed it (the authenticated Supabase user id, taken server side from
-- the caller's token), when, and the old and new values (NULL = inherit
-- MERGE_POLICY). Read through GET /repositories/{id}/merge-policy/history.
--
-- The policy update and its audit row are written together by
-- set_repository_merge_policy() below, in one transaction: there is never a
-- policy change without its row, or a row without the change. The function
-- takes the actor as an argument because it is called by the backend with
-- the service role, where auth.uid() is NULL. Setting the value the
-- repository already has changes nothing and writes no row.
--
-- Append-only: org members read their organization's rows; anon and
-- authenticated have no INSERT/UPDATE/DELETE policy, so RLS rejects their
-- writes, and a trigger rejects UPDATE from every role. DELETE stays
-- possible for the service role and for ON DELETE CASCADE, so deleting an
-- organization still removes its rows.
--
-- repository_id is the GitHub repository id without a foreign key (as in
-- merge_decisions): the history survives the repositories row being removed
-- and re-synced (the GitHub App uninstalled and installed again).
-- changed_by has no foreign key to auth.users either, so deleting a user
-- does not rewrite or remove the record of what they changed.
--
-- Re-runnable (IF NOT EXISTS, DROP ... IF EXISTS before CREATE, CREATE OR
-- REPLACE).

CREATE TABLE IF NOT EXISTS repository_merge_policy_changes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    repository_id BIGINT NOT NULL,                -- GitHub repository id (repositories.id)
    -- NULL = inherit MERGE_POLICY.
    old_policy TEXT
        CONSTRAINT repository_merge_policy_changes_old_policy_check
        CHECK (old_policy IS NULL OR old_policy IN ('human', 'ci', 'auto')),
    new_policy TEXT
        CONSTRAINT repository_merge_policy_changes_new_policy_check
        CHECK (new_policy IS NULL OR new_policy IN ('human', 'ci', 'auto')),
    changed_by UUID NOT NULL,                     -- auth.users.id of the caller
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT repository_merge_policy_changes_is_a_change
        CHECK (old_policy IS DISTINCT FROM new_policy)
);

CREATE INDEX IF NOT EXISTS repository_merge_policy_changes_repo_idx
    ON repository_merge_policy_changes(org_id, repository_id, changed_at DESC);

ALTER TABLE repository_merge_policy_changes ENABLE ROW LEVEL SECURITY;

-- Org members read their organization's rows.
DROP POLICY IF EXISTS "repository_merge_policy_changes_select"
    ON repository_merge_policy_changes;
CREATE POLICY "repository_merge_policy_changes_select"
    ON repository_merge_policy_changes
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

-- Only the backend writes. No INSERT, UPDATE or DELETE policy exists for
-- anon or authenticated, so RLS rejects them.
DROP POLICY IF EXISTS "repository_merge_policy_changes_insert_service"
    ON repository_merge_policy_changes;
CREATE POLICY "repository_merge_policy_changes_insert_service"
    ON repository_merge_policy_changes
    FOR INSERT TO service_role
    WITH CHECK (true);

GRANT ALL ON repository_merge_policy_changes TO service_role;

CREATE OR REPLACE FUNCTION repository_merge_policy_changes_append_only()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'repository_merge_policy_changes is append-only'
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS repository_merge_policy_changes_append_only
    ON repository_merge_policy_changes;
CREATE TRIGGER repository_merge_policy_changes_append_only
    BEFORE UPDATE ON repository_merge_policy_changes
    FOR EACH ROW EXECUTE FUNCTION repository_merge_policy_changes_append_only();

-- Set a repository's merge policy and record the change, atomically.
--
-- Returns the audit row as JSON (id, changed_by, changed_at, old_policy,
-- new_policy), or NULL when the repository already had p_merge_policy (no
-- change, no row). Raises P0002 (no_data_found) when the repository is not
-- in p_org_id, so another org's repository is indistinguishable from a
-- missing one. The row is locked (FOR UPDATE) so concurrent changes are
-- serialised and each row's old_policy is the value it replaced.
--
-- SECURITY INVOKER: it runs with the caller's rights, which only the
-- service role's suffice for (RLS and the merge_policy trigger reject
-- anon/authenticated). EXECUTE is revoked from them as well: the ceiling and
-- the caller's org role are checked by the API, not here.
CREATE OR REPLACE FUNCTION set_repository_merge_policy(
    p_repository_id BIGINT,
    p_org_id UUID,
    p_merge_policy TEXT,
    p_changed_by UUID
)
RETURNS JSONB LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
    v_old TEXT;
    v_change repository_merge_policy_changes%ROWTYPE;
BEGIN
    IF p_changed_by IS NULL THEN
        RAISE EXCEPTION 'p_changed_by is required'
            USING ERRCODE = 'null_value_not_allowed';
    END IF;

    SELECT merge_policy INTO v_old
    FROM repositories
    WHERE id = p_repository_id AND org_id = p_org_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'repository not found' USING ERRCODE = 'no_data_found';
    END IF;

    IF v_old IS NOT DISTINCT FROM p_merge_policy THEN
        RETURN NULL;
    END IF;

    UPDATE repositories SET merge_policy = p_merge_policy
    WHERE id = p_repository_id AND org_id = p_org_id;

    INSERT INTO repository_merge_policy_changes
        (org_id, repository_id, old_policy, new_policy, changed_by)
    VALUES (p_org_id, p_repository_id, v_old, p_merge_policy, p_changed_by)
    RETURNING * INTO v_change;

    RETURN jsonb_build_object(
        'id', v_change.id,
        'old_policy', v_change.old_policy,
        'new_policy', v_change.new_policy,
        'changed_by', v_change.changed_by,
        'changed_at', v_change.changed_at
    );
END;
$$;

REVOKE ALL ON FUNCTION set_repository_merge_policy(BIGINT, UUID, TEXT, UUID)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION set_repository_merge_policy(BIGINT, UUID, TEXT, UUID)
    TO service_role;
