-- ============================================================================
-- Merge decision audit rows (epic #411)
-- ============================================================================
--
-- One row per merge decision on an agent PR (backend/merge_audit.py, written
-- by agents.maintainer_node through the service-role client): merged, held
-- open for a human, or not merged. Each Maintainer review of a PR is a
-- decision event, so a run whose Maintainer requests changes and later
-- approves has one row per review. A redelivered run that replays a review
-- from its checkpoint writes nothing; one that has to review again (it
-- crashed before the checkpoint) records the new decision as a new row.
--
-- Written by the backend only: org members can read their organization's
-- rows, nobody but the service role can write them. Not added to the
-- supabase_realtime publication: the dashboard reads decisions through
-- GET /runs/{run_id}/decisions and does not subscribe to them.
--
-- Re-runnable (IF NOT EXISTS, DROP POLICY IF EXISTS before CREATE).

CREATE TABLE IF NOT EXISTS merge_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    run_id UUID REFERENCES runs(id) ON DELETE CASCADE,
    repository_id BIGINT,                         -- GitHub repository id (runs.repository_id)
    repo_name TEXT,                               -- "owner/name" at the time of the run
    pr_number INTEGER,
    head_sha TEXT NOT NULL,                       -- The reviewed head ('' when unknown)
    iteration INTEGER,                            -- Maintainer review iteration

    -- MERGE_POLICY in force when the decision was made.
    policy TEXT NOT NULL
        CONSTRAINT merge_decisions_policy_check
        CHECK (policy IN ('human', 'ci', 'auto')),
    -- merged: merged by AutoMaintainer; held: approved by the Maintainer but
    -- left open for a human; not_merged: rejected by the Maintainer, or
    -- GitHub refused the merge.
    decision TEXT NOT NULL
        CONSTRAINT merge_decisions_decision_check
        CHECK (decision IN ('merged', 'not_merged', 'held')),
    -- agent: the Maintainer's verdict decided (actor_role names it); policy:
    -- the merge policy or a safety gate decided after an approval; human:
    -- reserved for decisions taken by a person (not written by the backend).
    actor TEXT NOT NULL
        CONSTRAINT merge_decisions_actor_check
        CHECK (actor IN ('agent', 'policy', 'human')),
    actor_role TEXT
        CONSTRAINT merge_decisions_actor_role_length
        CHECK (char_length(actor_role) <= 100),
    -- Short reason strings, e.g. "maintainer rejected", "policy human".
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb
        CONSTRAINT merge_decisions_reasons_is_array
        -- CASE, not AND: jsonb_array_length raises on a non-array.
        CHECK (CASE WHEN jsonb_typeof(reasons) = 'array'
                    THEN jsonb_array_length(reasons) <= 20
                    ELSE false END),
    -- Links and ids backing the decision (PR, commit, verification row).
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CONSTRAINT merge_decisions_evidence_is_object
        CHECK (jsonb_typeof(evidence) = 'object'),

    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,

    -- Size caps, kept even if a writer forgets to truncate.
    CONSTRAINT merge_decisions_reasons_size CHECK (char_length(reasons::text) <= 4000),
    CONSTRAINT merge_decisions_evidence_size CHECK (char_length(evidence::text) <= 4000)
);

CREATE INDEX IF NOT EXISTS merge_decisions_run_idx
    ON merge_decisions(run_id);
CREATE INDEX IF NOT EXISTS merge_decisions_org_created_idx
    ON merge_decisions(org_id, created_at DESC);

ALTER TABLE merge_decisions ENABLE ROW LEVEL SECURITY;

-- Org members read their organization's rows.
DROP POLICY IF EXISTS "merge_decisions_select" ON merge_decisions;
CREATE POLICY "merge_decisions_select" ON merge_decisions
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

-- Only the backend writes (as for verification_results). No INSERT, UPDATE
-- or DELETE policy exists for anon or authenticated, so RLS rejects them.
DROP POLICY IF EXISTS "merge_decisions_insert_service" ON merge_decisions;
CREATE POLICY "merge_decisions_insert_service" ON merge_decisions
    FOR INSERT TO service_role
    WITH CHECK (true);

GRANT ALL ON merge_decisions TO service_role;
