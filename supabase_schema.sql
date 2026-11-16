-- ============================================================================
-- GENERATED FILE - DO NOT EDIT.
-- ============================================================================
-- The migrations in supabase/migrations, concatenated in version order by
--     python scripts/db-migrate schema
-- To change the schema, add a migration there and regenerate this file; CI
-- (.github/workflows/ci-db.yml) fails when it is out of date.
--
-- Prefer applying the migrations themselves (`supabase db push`, or
-- `python scripts/db-migrate up`), which record what was applied; see
-- docs/DEPLOYMENT.md, "Database". This single file is for pasting into the
-- Supabase SQL Editor. Every migration is re-runnable, so running this file
-- again on an existing database adds what is missing and keeps all data.

-- ============================================================================
-- supabase/migrations/20261116000000_baseline.sql
-- ============================================================================

-- ============================================================================
-- AutoMaintainer baseline schema (#473)
-- Multi-tenant SaaS Architecture with RLS, Billing, and GitHub App Support
-- ============================================================================
--
-- The schema as it stood when versioned migrations were introduced: the old
-- single-file supabase_schema.sql, including every block appended to it over
-- time (runs.checkpoint, runs.repo_name, service-role-only github_installations
-- writes, repositories_org_immutable, repository_in_org/run_in_org and the
-- tenant checks in the run and IDE policies).
--
-- Unlike that file it never drops a table, and every statement is guarded
-- (IF NOT EXISTS, OR REPLACE, DROP ... IF EXISTS before CREATE), so it can be
-- applied to:
--   * an empty database (new deployment);
--   * a database built from an earlier multi-tenant supabase_schema.sql
--     (one whose runs and logs tables already have org_id) -- it adds what is
--     missing (columns, functions, policies) and keeps every row. Databases
--     from the pre-multi-tenant schema (runs/logs without org_id) are not
--     supported: their rows have no organization to assign; see
--     docs/DEPLOYMENT.md "Upgrading";
--   * a database it was already applied to (a no-op).
-- Every later migration must keep that property: the RLS suite
-- (backend/tests/rls) applies all migrations a second time and fails if
-- anything errors, changes or loses data.

-- 0. EXTENSIONS
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- 1. ORGANIZATIONS & USERS (Multi-Tenancy Foundation)
-- ============================================================================

CREATE TABLE IF NOT EXISTS organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,                    -- URL-friendly identifier
    github_org_login TEXT UNIQUE,                 -- GitHub org/user login for webhook routing
    avatar_url TEXT,
    plan TEXT DEFAULT 'free' CHECK (plan IN ('free', 'pro', 'team', 'enterprise')),
    settings JSONB DEFAULT '{}',                  -- Feature flags, limits, preferences
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS orgs_slug_idx ON organizations(slug);
CREATE INDEX IF NOT EXISTS orgs_github_login_idx ON organizations(github_org_login);

-- Users are managed by Supabase Auth, but we track org membership here
CREATE TABLE IF NOT EXISTS organization_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL,                        -- References auth.users(id)
    role TEXT DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    invited_by UUID,                              -- References auth.users(id)
    invited_at TIMESTAMPTZ DEFAULT now(),
    joined_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    UNIQUE (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS org_members_user_idx ON organization_members(user_id);
CREATE INDEX IF NOT EXISTS org_members_org_idx ON organization_members(org_id);

-- ============================================================================
-- 2. GITHUB APP INSTALLATIONS (Per-org GitHub integration)
-- ============================================================================

CREATE TABLE IF NOT EXISTS github_installations (
    id BIGINT PRIMARY KEY,                        -- GitHub installation ID
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    account_login TEXT NOT NULL,                  -- GitHub org/user login
    account_type TEXT NOT NULL CHECK (account_type IN ('Organization', 'User')),
    account_avatar_url TEXT,
    target_type TEXT NOT NULL CHECK (target_type IN ('Organization', 'User', 'Repository')),
    repository_selection TEXT DEFAULT 'all' CHECK (repository_selection IN ('all', 'selected')),
    permissions JSONB NOT NULL,                   -- GitHub App permissions granted
    events JSONB NOT NULL,                        -- GitHub App events subscribed
    suspended_at TIMESTAMPTZ,                     -- Null = active
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS github_installations_org_idx ON github_installations(org_id);
CREATE INDEX IF NOT EXISTS github_installations_account_idx ON github_installations(account_login);

-- ============================================================================
-- 3. REPOSITORIES (Tracked repos per org)
-- ============================================================================

CREATE TABLE IF NOT EXISTS repositories (
    id BIGINT PRIMARY KEY,                        -- GitHub repository ID
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    github_installation_id BIGINT NOT NULL REFERENCES github_installations(id) ON DELETE CASCADE,
    full_name TEXT NOT NULL,                      -- owner/repo
    name TEXT NOT NULL,
    owner_login TEXT NOT NULL,
    owner_type TEXT NOT NULL CHECK (owner_type IN ('Organization', 'User')),
    private BOOLEAN DEFAULT false,
    default_branch TEXT DEFAULT 'main',
    description TEXT,
    language TEXT,
    topics JSONB DEFAULT '[]',
    avatar_url TEXT,
    html_url TEXT,
    archived BOOLEAN DEFAULT false,
    disabled BOOLEAN DEFAULT false,
    pushed_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    UNIQUE (org_id, full_name)
);

CREATE INDEX IF NOT EXISTS repos_org_idx ON repositories(org_id);
CREATE INDEX IF NOT EXISTS repos_installation_idx ON repositories(github_installation_id);
CREATE INDEX IF NOT EXISTS repos_full_name_idx ON repositories(full_name);

-- ============================================================================
-- 4. AGENT RUNS (Core execution tracking - now multi-tenant)
-- ============================================================================

CREATE TABLE IF NOT EXISTS runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL,                        -- Who triggered the run (auth.users)
    repository_id BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    github_installation_id BIGINT NOT NULL REFERENCES github_installations(id) ON DELETE CASCADE,

    -- Run configuration
    target_issue_number INTEGER,                  -- Optional: target specific issue
    target_branch TEXT,                           -- Optional: target specific branch
    mode TEXT DEFAULT 'autonomous' CHECK (mode IN ('autonomous', 'targeted_issue', 'code_review', 'refactor')),

    -- Execution state
    status TEXT DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'awaiting_review')),
    current_agent TEXT CHECK (current_agent IN ('architect', 'visionary', 'reviewer', 'implementer', 'maintainer')),
    iteration INTEGER DEFAULT 0,
    max_iterations INTEGER DEFAULT 3,

    -- Task queue tracking
    celery_task_id TEXT,                          -- Celery task handling this run

    -- GitHub artifacts created
    github_issue_number INTEGER,
    github_pr_number INTEGER,
    github_branch_name TEXT,
    github_commit_sha TEXT,

    -- Results
    result_summary TEXT,
    error_message TEXT,

    -- Timing
    queued_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS runs_org_idx ON runs(org_id);
CREATE INDEX IF NOT EXISTS runs_user_idx ON runs(user_id);
CREATE INDEX IF NOT EXISTS runs_repo_idx ON runs(repository_id);
CREATE INDEX IF NOT EXISTS runs_status_idx ON runs(status);
CREATE INDEX IF NOT EXISTS runs_created_idx ON runs(created_at DESC);

-- Step checkpoints (issue_number, branch_name, pr_number, merge state, ...)
-- written by the agent graph so a redelivered Celery task resumes the run
-- instead of repeating GitHub side effects.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb;

-- "owner/name" of the run's repository, denormalized for the dashboard and
-- the stale-run reaper. POST /start, the agent loop and tasks.py write or
-- read it; without it every /start insert fails.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS repo_name TEXT;

-- ============================================================================
-- 5. AGENT LOGS (Real-time streaming - now multi-tenant)
-- ============================================================================

CREATE TABLE IF NOT EXISTS logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    user_id UUID,                                 -- For user-specific logs (optional)

    agent_name TEXT NOT NULL,
    log_type TEXT DEFAULT 'message' CHECK (log_type IN ('message', 'ui_update', 'tool_call', 'tool_result', 'error', 'metric')),
    message TEXT,
    color TEXT,                                   -- Tailwind color class for UI
    metadata JSONB,                               -- Structured data: {systemHealth, agentStatus, pipeline, activity, ...}

    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS logs_run_id_created_idx ON logs(run_id, created_at ASC);
CREATE INDEX IF NOT EXISTS logs_org_created_idx ON logs(org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS logs_type_idx ON logs(log_type);

-- ============================================================================
-- 6. USAGE EVENTS (Billing & Metering)
-- ============================================================================

CREATE TABLE IF NOT EXISTS usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID,                                 -- Who triggered (optional)
    run_id UUID REFERENCES runs(id) ON DELETE SET NULL,

    -- Event classification
    event_type TEXT NOT NULL CHECK (event_type IN (
        'agent_run_started',
        'agent_run_completed',
        'agent_run_failed',
        'llm_tokens_consumed',
        'github_api_calls',
        'webhook_received',
        'ide_session_started',
        'ide_session_ended'
    )),

    -- Quantities for billing
    quantity INTEGER DEFAULT 1,
    unit TEXT DEFAULT 'count',                    -- 'count', 'tokens', 'seconds', 'mb'

    -- Cost tracking (in USD cents for precision)
    estimated_cost_cents INTEGER DEFAULT 0,
    actual_cost_cents INTEGER,

    -- Metadata
    metadata JSONB DEFAULT '{}',
    model_used TEXT,                              -- e.g., 'llama-3.3-70b-versatile'
    provider TEXT DEFAULT 'groq',

    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS usage_org_created_idx ON usage_events(org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_run_idx ON usage_events(run_id);
CREATE INDEX IF NOT EXISTS usage_type_created_idx ON usage_events(event_type, created_at DESC);

-- Monthly aggregation view for billing
-- security_invoker = true ensures the querying user's RLS policies
-- are enforced on the underlying tables, not the view owner's.
-- DROP + CREATE rather than CREATE OR REPLACE: a view left by an older schema
-- may lack security_invoker or have different columns, which OR REPLACE
-- cannot change. Views hold no data.
DROP VIEW IF EXISTS monthly_usage_summary;
CREATE VIEW monthly_usage_summary
WITH (security_invoker = true) AS
SELECT
    org_id,
    date_trunc('month', created_at) AS month,
    event_type,
    SUM(quantity) AS total_quantity,
    SUM(estimated_cost_cents) AS total_estimated_cost_cents,
    COUNT(DISTINCT run_id) AS runs_count
FROM usage_events
GROUP BY org_id, date_trunc('month', created_at), event_type;

-- ============================================================================
-- 7. WEBHOOK EVENTS (GitHub webhook processing queue)
-- ============================================================================

CREATE TABLE IF NOT EXISTS webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    github_installation_id BIGINT NOT NULL REFERENCES github_installations(id) ON DELETE CASCADE,

    -- GitHub event metadata
    github_delivery_id TEXT NOT NULL,
    event_type TEXT NOT NULL,                     -- e.g., 'pull_request', 'issues', 'push'
    action TEXT,                                  -- e.g., 'opened', 'closed', 'synchronize'

    -- Payload
    payload JSONB NOT NULL,

    -- Processing state
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'ignored')),
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,

    -- Timing
    received_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS webhook_org_received_idx ON webhook_events(org_id, received_at DESC);
CREATE INDEX IF NOT EXISTS webhook_status_idx ON webhook_events(status);
CREATE INDEX IF NOT EXISTS webhook_delivery_idx ON webhook_events(github_delivery_id);
CREATE UNIQUE INDEX IF NOT EXISTS webhook_dedup_idx ON webhook_events(github_delivery_id) WHERE status != 'ignored';

-- ============================================================================
-- 8. IDE SESSIONS (WebIDE collaboration tracking)
-- ============================================================================

CREATE TABLE IF NOT EXISTS ide_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL,
    run_id UUID REFERENCES runs(id) ON DELETE SET NULL,
    repository_id BIGINT REFERENCES repositories(id) ON DELETE SET NULL,

    -- Session state
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'idle', 'ended')),
    current_branch TEXT,
    staged_changes JSONB DEFAULT '{}',            -- File path -> content for preview

    -- Timing
    started_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    last_activity_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS ide_sessions_org_user_idx ON ide_sessions(org_id, user_id);
CREATE INDEX IF NOT EXISTS ide_sessions_run_idx ON ide_sessions(run_id);
CREATE INDEX IF NOT EXISTS ide_sessions_status_idx ON ide_sessions(status);

-- ============================================================================
-- 9. ROW LEVEL SECURITY (RLS) POLICIES
-- ============================================================================

-- Enable RLS on all tables
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE github_installations ENABLE ROW LEVEL SECURITY;
ALTER TABLE repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE ide_sessions ENABLE ROW LEVEL SECURITY;

-- Helper function: current user's org membership
-- SECURITY DEFINER so these helpers read organization_members directly
-- without re-triggering its RLS policies (prevents SQLSTATE 42P17
-- infinite-recursion errors on policy evaluation).
CREATE OR REPLACE FUNCTION current_user_org_ids()
RETURNS UUID[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, auth AS $$
    SELECT array_agg(org_id)
    FROM organization_members
    WHERE user_id = auth.uid() AND joined_at IS NOT NULL;
$$;

-- Helper: the repository (and, when given, the installation it is linked
-- through) belongs to org_uuid. SECURITY DEFINER so policies can check rows
-- the caller cannot see (another org's repository must make this false, not
-- be invisible and pass).
CREATE OR REPLACE FUNCTION repository_in_org(repo_id BIGINT, org_uuid UUID, installation_id BIGINT DEFAULT NULL)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (
        SELECT 1 FROM repositories r
        WHERE r.id = repo_id
          AND r.org_id = org_uuid
          AND (installation_id IS NULL OR r.github_installation_id = installation_id)
    );
$$;

-- Helper: the run belongs to org_uuid (for ide_sessions.run_id).
CREATE OR REPLACE FUNCTION run_in_org(run_uuid UUID, org_uuid UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM runs WHERE id = run_uuid AND org_id = org_uuid);
$$;

-- Helper function: user is org admin/owner
CREATE OR REPLACE FUNCTION user_is_org_admin(org_uuid UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, auth AS $$
    SELECT EXISTS (
        SELECT 1 FROM organization_members
        WHERE org_id = org_uuid
        AND user_id = auth.uid()
        AND role IN ('owner', 'admin')
        AND joined_at IS NOT NULL
    );
$$;

-- ============================================================================
-- RLS POLICIES
-- ============================================================================
-- Every policy is dropped (if present) and created again, so a re-run or an
-- older database ends up with exactly these definitions.

-- Organizations: users see orgs they're members of
DROP POLICY IF EXISTS "orgs_select_member" ON organizations;
CREATE POLICY "orgs_select_member" ON organizations
    FOR SELECT TO authenticated
    USING (id = ANY(current_user_org_ids()));

DROP POLICY IF EXISTS "orgs_insert_owner" ON organizations;
CREATE POLICY "orgs_insert_owner" ON organizations
    FOR INSERT TO authenticated
    WITH CHECK (true);  -- Any authenticated user can create org (becomes owner via trigger)

DROP POLICY IF EXISTS "orgs_update_admin" ON organizations;
CREATE POLICY "orgs_update_admin" ON organizations
    FOR UPDATE TO authenticated
    USING (user_is_org_admin(id))
    WITH CHECK (user_is_org_admin(id));

-- Organization Members: users see members of their orgs
DROP POLICY IF EXISTS "org_members_select" ON organization_members;
CREATE POLICY "org_members_select" ON organization_members
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

DROP POLICY IF EXISTS "org_members_insert_admin" ON organization_members;
CREATE POLICY "org_members_insert_admin" ON organization_members
    FOR INSERT TO authenticated
    WITH CHECK (user_is_org_admin(org_id));

DROP POLICY IF EXISTS "org_members_update_admin" ON organization_members;
CREATE POLICY "org_members_update_admin" ON organization_members
    FOR UPDATE TO authenticated
    USING (user_is_org_admin(org_id))
    WITH CHECK (user_is_org_admin(org_id));

DROP POLICY IF EXISTS "org_members_delete_admin" ON organization_members;
CREATE POLICY "org_members_delete_admin" ON organization_members
    FOR DELETE TO authenticated
    USING (user_is_org_admin(org_id));

-- GitHub Installations: visible to org members
DROP POLICY IF EXISTS "github_install_select" ON github_installations;
CREATE POLICY "github_install_select" ON github_installations
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

-- Installations are linked only by the backend (service role), after
-- POST /github/installations/claim has verified with GitHub that the caller
-- controls the installation. Client-side insert/update used to be allowed for
-- any org admin, which let one tenant claim another tenant's installation id
-- (and, via repository sync, its repositories). Older databases still have
-- those policies; the first two DROPs remove them.
DROP POLICY IF EXISTS "github_install_insert_admin" ON github_installations;
DROP POLICY IF EXISTS "github_install_update_admin" ON github_installations;

DROP POLICY IF EXISTS "github_install_insert_service" ON github_installations;
CREATE POLICY "github_install_insert_service" ON github_installations
    FOR INSERT TO service_role
    WITH CHECK (true);

DROP POLICY IF EXISTS "github_install_update_service" ON github_installations;
CREATE POLICY "github_install_update_service" ON github_installations
    FOR UPDATE TO service_role
    USING (true);

-- Repositories: visible to org members
DROP POLICY IF EXISTS "repos_select" ON repositories;
CREATE POLICY "repos_select" ON repositories
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

DROP POLICY IF EXISTS "repos_insert_service" ON repositories;
CREATE POLICY "repos_insert_service" ON repositories
    FOR INSERT TO service_role
    WITH CHECK (true);

DROP POLICY IF EXISTS "repos_update_service" ON repositories;
CREATE POLICY "repos_update_service" ON repositories
    FOR UPDATE TO service_role
    USING (true);

-- Runs: users see runs in their orgs
DROP POLICY IF EXISTS "runs_select" ON runs;
CREATE POLICY "runs_select" ON runs
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

-- A run's repository and installation must belong to the run's org, so a
-- member cannot attach another org's repository to a run in their own org.
DROP POLICY IF EXISTS "runs_insert_own" ON runs;
CREATE POLICY "runs_insert_own" ON runs
    FOR INSERT TO authenticated
    WITH CHECK (
        org_id = ANY(current_user_org_ids())
        AND user_id = auth.uid()
        AND repository_in_org(repository_id, org_id, github_installation_id)
    );

DROP POLICY IF EXISTS "runs_update_own" ON runs;
CREATE POLICY "runs_update_own" ON runs
    FOR UPDATE TO authenticated
    USING (org_id = ANY(current_user_org_ids()) AND user_id = auth.uid())
    WITH CHECK (
        org_id = ANY(current_user_org_ids())
        AND repository_in_org(repository_id, org_id, github_installation_id)
    );

-- Service role bypasses all (for backend workers)
DROP POLICY IF EXISTS "runs_service_all" ON runs;
CREATE POLICY "runs_service_all" ON runs
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- Logs: users see logs for runs in their orgs
DROP POLICY IF EXISTS "logs_select" ON logs;
CREATE POLICY "logs_select" ON logs
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

DROP POLICY IF EXISTS "logs_insert_service" ON logs;
CREATE POLICY "logs_insert_service" ON logs
    FOR INSERT TO service_role
    WITH CHECK (true);

-- Usage Events: org admins see all, users see own
DROP POLICY IF EXISTS "usage_select_admin" ON usage_events;
CREATE POLICY "usage_select_admin" ON usage_events
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()) AND user_is_org_admin(org_id));

DROP POLICY IF EXISTS "usage_select_own" ON usage_events;
CREATE POLICY "usage_select_own" ON usage_events
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

DROP POLICY IF EXISTS "usage_insert_service" ON usage_events;
CREATE POLICY "usage_insert_service" ON usage_events
    FOR INSERT TO service_role
    WITH CHECK (true);

-- Webhook Events: service role only (internal processing)
DROP POLICY IF EXISTS "webhook_service_all" ON webhook_events;
CREATE POLICY "webhook_service_all" ON webhook_events
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- IDE Sessions: users see own sessions, org admins see all
DROP POLICY IF EXISTS "ide_select_own" ON ide_sessions;
CREATE POLICY "ide_select_own" ON ide_sessions
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

DROP POLICY IF EXISTS "ide_select_admin" ON ide_sessions;
CREATE POLICY "ide_select_admin" ON ide_sessions
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()) AND user_is_org_admin(org_id));

-- An IDE session stays in one of the caller's orgs, and its run and
-- repository (when set) belong to that same org.
DROP POLICY IF EXISTS "ide_insert_own" ON ide_sessions;
CREATE POLICY "ide_insert_own" ON ide_sessions
    FOR INSERT TO authenticated
    WITH CHECK (
        user_id = auth.uid()
        AND org_id = ANY(current_user_org_ids())
        AND (repository_id IS NULL OR repository_in_org(repository_id, org_id))
        AND (run_id IS NULL OR run_in_org(run_id, org_id))
    );

DROP POLICY IF EXISTS "ide_update_own" ON ide_sessions;
CREATE POLICY "ide_update_own" ON ide_sessions
    FOR UPDATE TO authenticated
    USING (user_id = auth.uid() AND org_id = ANY(current_user_org_ids()))
    WITH CHECK (
        user_id = auth.uid()
        AND org_id = ANY(current_user_org_ids())
        AND (repository_id IS NULL OR repository_in_org(repository_id, org_id))
        AND (run_id IS NULL OR run_in_org(run_id, org_id))
    );

-- ============================================================================
-- 10. TRIGGERS & FUNCTIONS
-- ============================================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS update_orgs_updated_at ON organizations;
CREATE TRIGGER update_orgs_updated_at BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_github_installations_updated_at ON github_installations;
CREATE TRIGGER update_github_installations_updated_at BEFORE UPDATE ON github_installations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_repos_updated_at ON repositories;
CREATE TRIGGER update_repos_updated_at BEFORE UPDATE ON repositories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_runs_updated_at ON runs;
CREATE TRIGGER update_runs_updated_at BEFORE UPDATE ON runs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- A repository never changes organization (epic #412). Repository sync
-- upserts on the GitHub repository id; it checks ownership first, but two
-- syncs of different orgs covering the same repository can both pass that
-- check before either writes. This trigger makes the losing write fail,
-- for the service role too, so a row can never be moved to another tenant
-- by a race (or by any other code path). github_app.sync_installation_
-- repositories recognises the 'repository_org_immutable' message and skips
-- the repository. Moving a repository between orgs deliberately means
-- deleting its row first.
CREATE OR REPLACE FUNCTION prevent_repository_org_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.org_id IS DISTINCT FROM OLD.org_id THEN
        RAISE EXCEPTION 'repository_org_immutable: repository % belongs to another organization', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS repositories_org_immutable ON repositories;
CREATE TRIGGER repositories_org_immutable BEFORE UPDATE OF org_id ON repositories
    FOR EACH ROW EXECUTE FUNCTION prevent_repository_org_change();

-- Auto-create organization_members entry when user creates org
CREATE OR REPLACE FUNCTION handle_new_organization()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO organization_members (org_id, user_id, role, joined_at)
    VALUES (NEW.id, auth.uid(), 'owner', now())
    ON CONFLICT (org_id, user_id) DO UPDATE SET role = 'owner', joined_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_org_created ON organizations;
CREATE TRIGGER on_org_created AFTER INSERT ON organizations
    FOR EACH ROW EXECUTE FUNCTION handle_new_organization();

-- Auto-create organization for new Supabase Auth users (first-time setup)
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    org_slug TEXT;
    org_name TEXT;
BEGIN
    -- Create personal org for user
    org_slug := 'personal-' || substr(NEW.id::text, 1, 8);
    org_name := COALESCE(NEW.raw_user_meta_data->>'full_name', NEW.email, 'User') || '''s Workspace';

    INSERT INTO organizations (name, slug, github_org_login)
    VALUES (org_name, org_slug, NEW.raw_user_meta_data->>'preferred_username')
    ON CONFLICT (slug) DO NOTHING;

    RETURN NEW;
END;
$$;

-- Note: This trigger would be on auth.users, which requires Supabase dashboard setup
-- For now, org creation happens via API endpoint

-- Helper: get a user's display name for a specific org (no email exposure).
-- SECURITY DEFINER so it can read auth.users and organization_members
-- directly, but only when the caller shares the org (enforced by
-- WHERE clause using current_user_org_ids() which is itself SECURITY DEFINER).
CREATE OR REPLACE FUNCTION org_member_display_name(p_user_id UUID, p_org_id UUID)
RETURNS TEXT LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, auth AS $$
    SELECT COALESCE(u.raw_user_meta_data->>'full_name', u.email, 'User')
    FROM auth.users u
    JOIN organization_members om ON om.user_id = u.id
    WHERE om.org_id = p_org_id
      AND om.user_id = p_user_id
      AND om.joined_at IS NOT NULL
      AND p_org_id = ANY(current_user_org_ids());
$$;

-- ============================================================================
-- 11. REALTIME PUBLICATION
-- ============================================================================

-- Add the tables the dashboard subscribes to. The old schema dropped and
-- re-created supabase_realtime, which also removed any table an operator had
-- added to it; this only creates the publication when it is missing and adds
-- the tables that are not in it yet.
DO $$
DECLARE
    t TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        CREATE PUBLICATION supabase_realtime;
    END IF;
    IF (SELECT puballtables FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        RETURN;  -- FOR ALL TABLES already covers them
    END IF;
    FOREACH t IN ARRAY ARRAY['logs', 'runs', 'webhook_events', 'ide_sessions'] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_publication_tables
            WHERE pubname = 'supabase_realtime' AND schemaname = 'public' AND tablename = t
        ) THEN
            EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', t);
        END IF;
    END LOOP;
END
$$;

-- ============================================================================
-- 12. HELPER VIEWS FOR ADMIN DASHBOARD
-- ============================================================================

-- Org health overview
DROP VIEW IF EXISTS org_health;
CREATE VIEW org_health
WITH (security_invoker = true) AS
SELECT
    o.id AS org_id,
    o.name AS org_name,
    o.slug,
    o.plan,
    COUNT(DISTINCT om.user_id) AS member_count,
    COUNT(DISTINCT r.id) AS repo_count,
    COUNT(DISTINCT runs.id) FILTER (WHERE runs.status = 'running') AS active_runs,
    COUNT(DISTINCT runs.id) FILTER (WHERE runs.created_at > now() - interval '24 hours') AS runs_24h,
    COUNT(DISTINCT runs.id) FILTER (WHERE runs.status = 'failed' AND runs.created_at > now() - interval '24 hours') AS failed_24h,
    COALESCE(SUM(ue.estimated_cost_cents) FILTER (WHERE ue.created_at > now() - interval '30 days'), 0) / 100.0 AS estimated_cost_30d_usd,
    MAX(runs.created_at) AS last_run_at
FROM organizations o
LEFT JOIN organization_members om ON om.org_id = o.id AND om.joined_at IS NOT NULL
LEFT JOIN repositories r ON r.org_id = o.id
LEFT JOIN runs ON runs.org_id = o.id
LEFT JOIN usage_events ue ON ue.org_id = o.id
GROUP BY o.id, o.name, o.slug, o.plan;

-- Recent runs with details (no email exposure; display name via helper).
-- runs.* is expanded when the view is created, so it must be re-created
-- (not replaced) after columns are added to runs.
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

-- ============================================================================
-- 13. GRANTS FOR SERVICE ROLE (Backend Workers)
-- ============================================================================

GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT USAGE ON SCHEMA public TO service_role;

-- ============================================================================
-- supabase/migrations/20261116120000_verification_results.sql
-- ============================================================================

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

-- ============================================================================
-- supabase/migrations/20261116180000_merge_decisions.sql
-- ============================================================================

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
