-- ============================================================================
-- Verification audit rows (epic #411)
-- ============================================================================
--
-- One row per verification of an agent change (backend/verification.py,
-- written by agents._verify_and_report through the service-role client). The
-- PR body and the automaintainer/verification commit status show only the
-- latest result; this table keeps every one, including the re-verification a
-- redelivered run does of a head that was already verified (a new test run,
-- so a new row; rows are never deduplicated).
--
-- Written by the backend only: org members can read their organization's
-- rows, nobody but the service role can write them. Not added to the
-- supabase_realtime publication: the dashboard reads results through
-- GET /runs/{run_id}/verifications and does not subscribe to them.
--
-- Re-runnable (IF NOT EXISTS, DROP POLICY IF EXISTS before CREATE).

CREATE TABLE IF NOT EXISTS verification_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    run_id UUID REFERENCES runs(id) ON DELETE CASCADE,
    repository_id BIGINT,                         -- GitHub repository id (runs.repository_id)
    repo_name TEXT,                               -- "owner/name" at the time of the run
    pr_number INTEGER,
    head_sha TEXT NOT NULL,                       -- The verified commit ('' when it could not be resolved)
    iteration INTEGER,                            -- Implementer iteration that pushed head_sha

    -- verification.VerificationResult
    status TEXT NOT NULL
        CONSTRAINT verification_results_status_check
        CHECK (status IN ('passed', 'failed', 'error', 'skipped')),
    summary TEXT
        CONSTRAINT verification_results_summary_length
        CHECK (char_length(summary) <= 1000),
    command JSONB                                 -- argv, never a shell string
        CONSTRAINT verification_results_command_is_array
        CHECK (jsonb_typeof(command) = 'array'),
    exit_code INTEGER,
    duration_ms INTEGER,
    -- Last part of the test output, already truncated by the backend
    -- (verification.AUDIT_OUTPUT_TAIL_CHARS); the CHECK keeps the cap.
    output_tail TEXT
        CONSTRAINT verification_results_output_tail_length
        CHECK (char_length(output_tail) <= 4000),
    output_truncated BOOLEAN NOT NULL DEFAULT false,
    backend TEXT,                                 -- 'local' or the SANDBOX_BACKEND factory

    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS verification_results_run_idx
    ON verification_results(run_id);
CREATE INDEX IF NOT EXISTS verification_results_org_created_idx
    ON verification_results(org_id, created_at DESC);

ALTER TABLE verification_results ENABLE ROW LEVEL SECURITY;

-- Org members read their organization's rows.
DROP POLICY IF EXISTS "verification_results_select" ON verification_results;
CREATE POLICY "verification_results_select" ON verification_results
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

-- Only the backend writes (as for usage_events and logs). No INSERT, UPDATE
-- or DELETE policy exists for anon or authenticated, so RLS rejects them.
DROP POLICY IF EXISTS "verification_results_insert_service" ON verification_results;
CREATE POLICY "verification_results_insert_service" ON verification_results
    FOR INSERT TO service_role
    WITH CHECK (true);

GRANT ALL ON verification_results TO service_role;
