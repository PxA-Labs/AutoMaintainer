-- ============================================================================
-- Run trigger attribution and one active run per issue (#518)
-- ============================================================================
--
-- 1. Trigger columns. runs.user_id is the AutoMaintainer user the run is
--    attributed to: the caller of POST /start, or the org's first owner for a
--    run started by the issue trigger label (webhook_processor.py), whose
--    adder need not have an AutoMaintainer account. These columns record who
--    actually started the run, as the worker's trust gate (backend/trust.py)
--    sees it:
--      trigger_source     'api' (POST /start) or 'label' (trigger label);
--                         no CHECK, so a new trigger path needs no migration
--      trigger_github_id  numeric GitHub user id (NULL: unknown, e.g. a
--                         /start caller without a linked GitHub identity)
--      trigger_login      GitHub login at the time of the trigger (display
--                         only; logins can change, the id is authoritative)
--    All nullable: rows written before this migration have none. They are
--    informational: the merge decision uses the values the worker receives
--    with the task, not these columns. Members can still insert and update
--    their own runs (runs_insert_own / runs_update_own), so for rows written
--    by a client these values are only as trustworthy as that client.
--    runs.user_id stays NOT NULL: the run RLS policies, the usage rows and
--    recent_runs_detailed.user_display_name all key on it.
--
-- 2. runs_one_active_per_issue: at most one queued, running or
--    awaiting_review run per (repository_id, target_issue_number). Those are
--    the statuses outside backend/tasks.py TERMINAL_RUN_STATUSES (completed,
--    failed, cancelled) and the runs.status CHECK. Runs without a target
--    issue are not constrained. The backend's own "is a run already active?"
--    check and its insert are separate statements; this index makes the rule
--    hold when two inserts race (two webhook deliveries, or /start and a
--    label). The label path records the losing delivery as ignored, /start
--    answers 409 RUN_ALREADY_ACTIVE.
--
--    Existing duplicates would make CREATE UNIQUE INDEX fail and the whole
--    migration roll back, so they are resolved first, in this transaction:
--    per (repository, issue) one active run is kept and every other active
--    one is marked 'cancelled' with an error_message saying why. The kept run
--    is the one furthest along: running first (a worker holds it; the
--    repository lock lets only one run per repository be running), then
--    awaiting_review (its PR exists), then the oldest queued run. A cancelled
--    queued run is skipped when its task is delivered (tasks.py skips runs in
--    a terminal status); a cancelled awaiting_review run leaves its PR open
--    for a human. Skipping the index instead would leave the race open with
--    no signal, and failing the migration would block deploys on data only
--    an operator can fix. Before this release such duplicates could only come
--    from that race or from repeated /start calls for the same issue.
--
-- 3. recent_runs_detailed selects runs.*, which PostgreSQL expands when the
--    view is created; it is re-created (as in the baseline) so it shows the
--    new columns.
--
-- Built without CONCURRENTLY: the runner applies each migration in one
-- transaction, and runs is a small table. Re-runnable (IF NOT EXISTS; the
-- UPDATE finds nothing once the index exists; DROP VIEW IF EXISTS).

ALTER TABLE runs ADD COLUMN IF NOT EXISTS trigger_source TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS trigger_github_id BIGINT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS trigger_login TEXT;

WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY repository_id, target_issue_number
            ORDER BY
                CASE status
                    WHEN 'running' THEN 0
                    WHEN 'awaiting_review' THEN 1
                    ELSE 2
                END,
                created_at,
                id
        ) AS position
    FROM runs
    WHERE status IN ('queued', 'running', 'awaiting_review')
      AND target_issue_number IS NOT NULL
)
UPDATE runs
SET status = 'cancelled',
    completed_at = COALESCE(runs.completed_at, now()),
    error_message = 'Cancelled by migration 20261117140000: another run for '
        || 'this issue was already queued, running or awaiting review'
FROM ranked
WHERE runs.id = ranked.id
  AND ranked.position > 1;

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_per_issue
    ON runs(repository_id, target_issue_number)
    WHERE status IN ('queued', 'running', 'awaiting_review')
      AND target_issue_number IS NOT NULL;

DROP VIEW IF EXISTS recent_runs_detailed;
CREATE VIEW recent_runs_detailed
WITH (security_invoker = true) AS
SELECT
    runs.*,
    repos.full_name AS repo_full_name,
    repos.private AS repo_private,
    org_member_display_name(runs.user_id, runs.org_id) AS user_display_name
FROM runs
JOIN repositories repos ON repos.id = runs.repository_id
WHERE runs.created_at > now() - interval '7 days'
ORDER BY runs.created_at DESC;

GRANT ALL ON recent_runs_detailed TO service_role;
