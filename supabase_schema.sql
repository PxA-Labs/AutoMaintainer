-- ============================================================================
-- AutoMaintainer Supabase Schema v2.0
-- Multi-tenant SaaS Architecture with RLS, Billing, and GitHub App Support
-- ============================================================================

-- 0. EXTENSIONS
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- 1. ORGANIZATIONS & USERS (Multi-Tenancy Foundation)
-- ============================================================================

CREATE TABLE organizations (
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

CREATE INDEX orgs_slug_idx ON organizations(slug);
CREATE INDEX orgs_github_login_idx ON organizations(github_org_login);

-- Users are managed by Supabase Auth, but we track org membership here
CREATE TABLE organization_members (
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

CREATE INDEX org_members_user_idx ON organization_members(user_id);
CREATE INDEX org_members_org_idx ON organization_members(org_id);

-- ============================================================================
-- 2. GITHUB APP INSTALLATIONS (Per-org GitHub integration)
-- ============================================================================

CREATE TABLE github_installations (
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

CREATE INDEX github_installations_org_idx ON github_installations(org_id);
CREATE INDEX github_installations_account_idx ON github_installations(account_login);

-- ============================================================================
-- 3. REPOSITORIES (Tracked repos per org)
-- ============================================================================

CREATE TABLE repositories (
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

CREATE INDEX repos_org_idx ON repositories(org_id);
CREATE INDEX repos_installation_idx ON repositories(github_installation_id);
CREATE INDEX repos_full_name_idx ON repositories(full_name);

-- ============================================================================
-- 4. AGENT RUNS (Core execution tracking - now multi-tenant)
-- ============================================================================

-- Drop old runs table if exists (migration)
DROP TABLE IF EXISTS runs CASCADE;

CREATE TABLE runs (
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

CREATE INDEX runs_org_idx ON runs(org_id);
CREATE INDEX runs_user_idx ON runs(user_id);
CREATE INDEX runs_repo_idx ON runs(repository_id);
CREATE INDEX runs_status_idx ON runs(status);
CREATE INDEX runs_created_idx ON runs(created_at DESC);

-- ============================================================================
-- 5. AGENT LOGS (Real-time streaming - now multi-tenant)
-- ============================================================================

DROP TABLE IF EXISTS logs CASCADE;

CREATE TABLE logs (
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

CREATE INDEX logs_run_id_created_idx ON logs(run_id, created_at ASC);
CREATE INDEX logs_org_created_idx ON logs(org_id, created_at DESC);
CREATE INDEX logs_type_idx ON logs(log_type);

-- ============================================================================
-- 6. USAGE EVENTS (Billing & Metering)
-- ============================================================================

CREATE TABLE usage_events (
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

CREATE INDEX usage_org_created_idx ON usage_events(org_id, created_at DESC);
CREATE INDEX usage_run_idx ON usage_events(run_id);
CREATE INDEX usage_type_created_idx ON usage_events(event_type, created_at DESC);

-- Monthly aggregation view for billing
CREATE VIEW monthly_usage_summary AS
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

CREATE TABLE webhook_events (
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

CREATE INDEX webhook_org_received_idx ON webhook_events(org_id, received_at DESC);
CREATE INDEX webhook_status_idx ON webhook_events(status);
CREATE INDEX webhook_delivery_idx ON webhook_events(github_delivery_id);
CREATE UNIQUE INDEX webhook_dedup_idx ON webhook_events(github_delivery_id) WHERE status != 'ignored';

-- ============================================================================
-- 8. IDE SESSIONS (WebIDE collaboration tracking)
-- ============================================================================

CREATE TABLE ide_sessions (
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

CREATE INDEX ide_sessions_org_user_idx ON ide_sessions(org_id, user_id);
CREATE INDEX ide_sessions_run_idx ON ide_sessions(run_id);
CREATE INDEX ide_sessions_status_idx ON ide_sessions(status);

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
CREATE OR REPLACE FUNCTION current_user_org_ids()
RETURNS UUID[] LANGUAGE sql STABLE AS $$
    SELECT array_agg(org_id) 
    FROM organization_members 
    WHERE user_id = auth.uid() AND joined_at IS NOT NULL;
$$;

-- Helper function: user is org admin/owner
CREATE OR REPLACE FUNCTION user_is_org_admin(org_uuid UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
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

-- Organizations: users see orgs they're members of
CREATE POLICY "orgs_select_member" ON organizations
    FOR SELECT TO authenticated
    USING (id = ANY(current_user_org_ids()));

CREATE POLICY "orgs_insert_owner" ON organizations
    FOR INSERT TO authenticated
    WITH CHECK (true);  -- Any authenticated user can create org (becomes owner via trigger)

CREATE POLICY "orgs_update_admin" ON organizations
    FOR UPDATE TO authenticated
    USING (user_is_org_admin(id))
    WITH CHECK (user_is_org_admin(id));

-- Organization Members: users see members of their orgs
CREATE POLICY "org_members_select" ON organization_members
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

CREATE POLICY "org_members_insert_admin" ON organization_members
    FOR INSERT TO authenticated
    WITH CHECK (user_is_org_admin(org_id));

CREATE POLICY "org_members_update_admin" ON organization_members
    FOR UPDATE TO authenticated
    USING (user_is_org_admin(org_id))
    WITH CHECK (user_is_org_admin(org_id));

CREATE POLICY "org_members_delete_admin" ON organization_members
    FOR DELETE TO authenticated
    USING (user_is_org_admin(org_id));

-- GitHub Installations: visible to org members
CREATE POLICY "github_install_select" ON github_installations
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

CREATE POLICY "github_install_insert_admin" ON github_installations
    FOR INSERT TO authenticated
    WITH CHECK (user_is_org_admin(org_id));

CREATE POLICY "github_install_update_admin" ON github_installations
    FOR UPDATE TO authenticated
    USING (user_is_org_admin(org_id))
    WITH CHECK (user_is_org_admin(org_id));

-- Repositories: visible to org members
CREATE POLICY "repos_select" ON repositories
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

CREATE POLICY "repos_insert_service" ON repositories
    FOR INSERT TO service_role
    WITH CHECK (true);

CREATE POLICY "repos_update_service" ON repositories
    FOR UPDATE TO service_role
    USING (true);

-- Runs: users see runs in their orgs
CREATE POLICY "runs_select" ON runs
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

CREATE POLICY "runs_insert_own" ON runs
    FOR INSERT TO authenticated
    WITH CHECK (org_id = ANY(current_user_org_ids()) AND user_id = auth.uid());

CREATE POLICY "runs_update_own" ON runs
    FOR UPDATE TO authenticated
    USING (org_id = ANY(current_user_org_ids()) AND user_id = auth.uid())
    WITH CHECK (org_id = ANY(current_user_org_ids()));

-- Service role bypasses all (for backend workers)
CREATE POLICY "runs_service_all" ON runs
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- Logs: users see logs for runs in their orgs
CREATE POLICY "logs_select" ON logs
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()));

CREATE POLICY "logs_insert_service" ON logs
    FOR INSERT TO service_role
    WITH CHECK (true);

-- Usage Events: org admins see all, users see own
CREATE POLICY "usage_select_admin" ON usage_events
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()) AND user_is_org_admin(org_id));

CREATE POLICY "usage_select_own" ON usage_events
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "usage_insert_service" ON usage_events
    FOR INSERT TO service_role
    WITH CHECK (true);

-- Webhook Events: service role only (internal processing)
CREATE POLICY "webhook_service_all" ON webhook_events
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- IDE Sessions: users see own sessions, org admins see all
CREATE POLICY "ide_select_own" ON ide_sessions
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "ide_select_admin" ON ide_sessions
    FOR SELECT TO authenticated
    USING (org_id = ANY(current_user_org_ids()) AND user_is_org_admin(org_id));

CREATE POLICY "ide_insert_own" ON ide_sessions
    FOR INSERT TO authenticated
    WITH CHECK (user_id = auth.uid() AND org_id = ANY(current_user_org_ids()));

CREATE POLICY "ide_update_own" ON ide_sessions
    FOR UPDATE TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

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

CREATE TRIGGER update_orgs_updated_at BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_github_installations_updated_at BEFORE UPDATE ON github_installations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_repos_updated_at BEFORE UPDATE ON repositories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_runs_updated_at BEFORE UPDATE ON runs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

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
    org_name := COALESCE(NEW.raw_user_meta_data->>'full_name', NEW.email, 'User') || '\'s Workspace';
    
    INSERT INTO organizations (name, slug, github_org_login)
    VALUES (org_name, org_slug, NEW.raw_user_meta_data->>'preferred_username')
    ON CONFLICT (slug) DO NOTHING;
    
    RETURN NEW;
END;
$$;

-- Note: This trigger would be on auth.users, which requires Supabase dashboard setup
-- For now, org creation happens via API endpoint

-- ============================================================================
-- 11. REALTIME PUBLICATION
-- ============================================================================

-- Reconfigure realtime for new tables
DROP PUBLICATION IF EXISTS supabase_realtime;
CREATE PUBLICATION supabase_realtime;
ALTER PUBLICATION supabase_realtime ADD TABLE logs, runs, webhook_events, ide_sessions;

-- ============================================================================
-- 12. HELPER VIEWS FOR ADMIN DASHBOARD
-- ============================================================================

-- Org health overview
CREATE VIEW org_health AS
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

-- Recent runs with details
CREATE VIEW recent_runs_detailed AS
SELECT 
    runs.*,
    repos.full_name AS repo_full_name,
    repos.private AS repo_private,
    u.email AS user_email,
    u.raw_user_meta_data->>'full_name' AS user_name
FROM runs
JOIN repositories repos ON repos.id = runs.repository_id
JOIN auth.users u ON u.id = runs.user_id
WHERE runs.created_at > now() - interval '7 days'
ORDER BY runs.created_at DESC;

-- ============================================================================
-- 13. GRANTS FOR SERVICE ROLE (Backend Workers)
-- ============================================================================

GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT USAGE ON SCHEMA public TO service_role;

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================