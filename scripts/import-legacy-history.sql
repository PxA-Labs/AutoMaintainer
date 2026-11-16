-- Import run history from a pre-multi-tenant database (#482).
--
-- For databases built from the earliest supabase_schema.sql, where runs and
-- logs have no org_id. That file exists in git history in one version only
-- (0147b09; the c398f6c revert restored it byte for byte, same blob
-- c4ba6be). The baseline migration cannot adopt such a database; this
-- procedure moves the old tables aside, lets the migrations create the
-- multi-tenant tables, and then copies the history into one organization.
--
-- Run it with psql (it uses psql variables and stops at the first error).
-- Every step runs in a single transaction: it either completes or changes
-- nothing. Every step can be run again.
--
--   1. Back up the database.
--   2. psql "$DATABASE_URL" -v step=prepare -f scripts/import-legacy-history.sql
--      Moves runs/logs (with their indexes and constraints) into the schema
--      automaintainer_legacy, drops their old "anonymous read" policies, any
--      views that depend on them (each definition is printed as a NOTICE so
--      you can re-create it) and their supabase_realtime membership, and
--      revokes the API roles' privileges. A rename to runs_legacy in
--      "public" would leave the rows readable through PostgREST with the old
--      USING (true) policy; automaintainer_legacy is not an exposed schema,
--      and the tables keep RLS enabled with no policies, so no API key can
--      read them even if it were.
--   3. Apply the migrations (scripts/db-migrate up, or supabase db push).
--   4. Create or choose the organization that will own the history, link its
--      GitHub App installation so its repositories are synced, then
--      psql "$DATABASE_URL" -v step=import -v org_id=<org uuid> \
--           [-v user_id=<member uuid>] -f scripts/import-legacy-history.sql
--   5. Check the history in the dashboard, then
--      psql "$DATABASE_URL" -v step=drop -f scripts/import-legacy-history.sql
--      which drops automaintainer_legacy only when every legacy row is in
--      the new tables.
--
-- Column mapping (legacy -> new), runs:
--   id -> id (kept, so logs.run_id needs no remapping)
--   repo_name -> repository_id, github_installation_id and repo_name of the
--     org's repository with that full name (case-insensitive; a stored
--     https://github.com/ URL or trailing .git is accepted). Legacy runs
--     have no repository id, and the new columns are NOT NULL, so the
--     import fails, listing the names, if a repository is not in the org.
--   target_issue -> target_issue_number; mode = targeted_issue when set,
--     otherwise autonomous
--   branch_name -> github_branch_name
--   status -> status, except 'running', which becomes 'failed' with an
--     error_message: the worker that owned such a run is gone, and as
--     'running' it would count as active and be picked up by
--     cleanup_stale_runs
--   created_at -> created_at, queued_at, started_at (the old worker inserted
--     the row when the run started); updated_at -> updated_at.
--     completed_at stays NULL: the old schema never recorded it.
--   user_id (new, NOT NULL) = :user_id, default the org's earliest owner.
-- logs:
--   id, run_id, agent_name, message, color, metadata, created_at -> same
--   org_id = :org_id; user_id = NULL
--   log_type -> log_type; a value the new CHECK does not allow becomes
--     'message', the original kept in metadata.legacy_log_type.
--
-- Rows already present (same primary key) are skipped, so a second import
-- inserts nothing. Importing into a different organization than the first
-- time is refused.

\set ON_ERROR_STOP on
\if :{?step}
\else
\set step ''
\endif
\if :{?org_id}
\else
\set org_id ''
\endif
\if :{?user_id}
\else
\set user_id ''
\endif

BEGIN;

-- psql does not interpolate variables inside the $$ bodies below; pass them
-- as transaction-local settings.
SET LOCAL automaintainer_legacy.step = :'step';
SET LOCAL automaintainer_legacy.org_id = :'org_id';
SET LOCAL automaintainer_legacy.user_id = :'user_id';

DO $$
BEGIN
    IF current_setting('automaintainer_legacy.step') NOT IN ('prepare', 'import', 'drop') THEN
        RAISE EXCEPTION 'set the step: psql -v step=prepare|import|drop -f scripts/import-legacy-history.sql';
    END IF;
END
$$;

-- Step 1 (before the migrations): move the legacy tables out of "public".
DO $$
DECLARE
    has_runs BOOLEAN := to_regclass('public.runs') IS NOT NULL;
    runs_has_org BOOLEAN;
    v RECORD;
    t TEXT;
    obj RECORD;
BEGIN
    IF current_setting('automaintainer_legacy.step') <> 'prepare' THEN
        RETURN;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'runs' AND column_name = 'org_id'
    ) INTO runs_has_org;

    IF to_regclass('automaintainer_legacy.runs') IS NOT NULL THEN
        RAISE NOTICE 'prepare: already done (automaintainer_legacy.runs exists); nothing to do';
        RETURN;
    END IF;
    IF NOT has_runs THEN
        RAISE NOTICE 'prepare: no public.runs table; nothing to do';
        RETURN;
    END IF;
    IF runs_has_org THEN
        RAISE NOTICE 'prepare: public.runs already has org_id (multi-tenant schema); '
            'nothing to do, apply the migrations directly';
        RETURN;
    END IF;
    IF to_regclass('public.logs') IS NULL THEN
        RAISE EXCEPTION 'prepare: public.runs has no org_id but public.logs is missing; '
            'this is not a database built from the pre-multi-tenant schema';
    END IF;

    -- A view follows the table it reads, not its name: left in place it would
    -- keep exposing the legacy rows through the API.
    FOR v IN
        SELECT DISTINCT vn.nspname AS schema_name, vc.relname AS view_name,
               vc.relkind, pg_get_viewdef(vc.oid) AS definition
        FROM pg_depend d
        JOIN pg_rewrite r ON r.oid = d.objid
        JOIN pg_class vc ON vc.oid = r.ev_class AND vc.relkind IN ('v', 'm')
        JOIN pg_namespace vn ON vn.oid = vc.relnamespace
        WHERE d.classid = 'pg_rewrite'::regclass
          AND d.refclassid = 'pg_class'::regclass
          AND d.refobjid IN ('public.runs'::regclass, 'public.logs'::regclass)
    LOOP
        RAISE NOTICE 'prepare: dropping view %.% (it reads the legacy tables): %',
            v.schema_name, v.view_name, v.definition;
        EXECUTE format('DROP %s IF EXISTS %I.%I CASCADE',
            CASE v.relkind WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'VIEW' END,
            v.schema_name, v.view_name);
    END LOOP;

    CREATE SCHEMA automaintainer_legacy;
    REVOKE ALL ON SCHEMA automaintainer_legacy FROM PUBLIC;

    FOREACH t IN ARRAY ARRAY['runs', 'logs'] LOOP
        FOR obj IN
            SELECT policyname AS name FROM pg_policies WHERE schemaname = 'public' AND tablename = t
        LOOP
            EXECUTE format('DROP POLICY %I ON public.%I', obj.name, t);
        END LOOP;
        -- Moving a table keeps its publications; supabase_realtime would
        -- keep streaming the legacy table.
        FOR obj IN
            SELECT p.pubname AS name FROM pg_publication_tables p
            JOIN pg_publication pub ON pub.pubname = p.pubname AND NOT pub.puballtables
            WHERE p.schemaname = 'public' AND p.tablename = t
        LOOP
            EXECUTE format('ALTER PUBLICATION %I DROP TABLE public.%I', obj.name, t);
        END LOOP;
        EXECUTE format('ALTER TABLE public.%I SET SCHEMA automaintainer_legacy', t);
        EXECUTE format('ALTER TABLE automaintainer_legacy.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON automaintainer_legacy.%I FROM PUBLIC', t);
    END LOOP;

    -- Supabase's API roles (absent on a plain PostgreSQL).
    FOR obj IN
        SELECT rolname AS name FROM pg_roles
        WHERE rolname IN ('anon', 'authenticated', 'service_role')
    LOOP
        EXECUTE format('REVOKE ALL ON SCHEMA automaintainer_legacy FROM %I', obj.name);
        EXECUTE format('REVOKE ALL ON automaintainer_legacy.runs, automaintainer_legacy.logs FROM %I',
            obj.name);
    END LOOP;

    RAISE NOTICE 'prepare: moved runs and logs to automaintainer_legacy; now apply the migrations';
END
$$;

-- Step 2 (after the migrations): copy the history into one organization.
DO $$
DECLARE
    org UUID;
    owner_id UUID;
    missing TEXT;
    foreign_ids BIGINT;
    runs_before BIGINT;
    logs_before BIGINT;
    runs_added BIGINT;
    logs_added BIGINT;
BEGIN
    IF current_setting('automaintainer_legacy.step') <> 'import' THEN
        RETURN;
    END IF;

    IF to_regclass('automaintainer_legacy.runs') IS NULL
       OR to_regclass('automaintainer_legacy.logs') IS NULL THEN
        RAISE EXCEPTION 'import: automaintainer_legacy.runs/logs not found; run -v step=prepare '
            'before applying the migrations';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'runs' AND column_name = 'org_id'
    ) THEN
        RAISE EXCEPTION 'import: public.runs is missing or has no org_id; apply the migrations first';
    END IF;

    -- The legacy schema version this mapping is written for (0147b09).
    SELECT string_agg(format('%s.%s', e.tbl, e.col), ', ' ORDER BY e.tbl, e.col)
    INTO missing
    FROM (VALUES
        ('runs', 'id'), ('runs', 'repo_name'), ('runs', 'target_issue'),
        ('runs', 'branch_name'), ('runs', 'status'), ('runs', 'created_at'),
        ('runs', 'updated_at'),
        ('logs', 'id'), ('logs', 'run_id'), ('logs', 'agent_name'), ('logs', 'log_type'),
        ('logs', 'message'), ('logs', 'color'), ('logs', 'metadata'), ('logs', 'created_at')
    ) AS e(tbl, col)
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns c
        WHERE c.table_schema = 'automaintainer_legacy' AND c.table_name = e.tbl
          AND c.column_name = e.col
    );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'import: unrecognised legacy schema, missing columns: %', missing;
    END IF;

    IF current_setting('automaintainer_legacy.org_id') = '' THEN
        RAISE EXCEPTION 'import: set the owning organization: -v org_id=<uuid>';
    END IF;
    org := current_setting('automaintainer_legacy.org_id')::uuid;
    IF NOT EXISTS (SELECT 1 FROM organizations WHERE id = org) THEN
        RAISE EXCEPTION 'import: organization % does not exist', org;
    END IF;

    IF current_setting('automaintainer_legacy.user_id') <> '' THEN
        owner_id := current_setting('automaintainer_legacy.user_id')::uuid;
        IF NOT EXISTS (
            SELECT 1 FROM organization_members WHERE org_id = org AND user_id = owner_id
        ) THEN
            RAISE EXCEPTION 'import: user % is not a member of organization %', owner_id, org;
        END IF;
    ELSE
        SELECT m.user_id INTO owner_id
        FROM organization_members m
        WHERE m.org_id = org AND m.role = 'owner'
        ORDER BY m.joined_at NULLS LAST, m.invited_at, m.user_id
        LIMIT 1;
        IF owner_id IS NULL THEN
            RAISE EXCEPTION 'import: organization % has no owner; pass -v user_id=<member uuid>', org;
        END IF;
    END IF;

    -- A second run must not move history to another organization.
    SELECT (SELECT count(*) FROM runs n JOIN automaintainer_legacy.runs l USING (id)
            WHERE n.org_id <> org)
         + (SELECT count(*) FROM logs n JOIN automaintainer_legacy.logs l USING (id)
            WHERE n.org_id <> org)
    INTO foreign_ids;
    IF foreign_ids > 0 THEN
        RAISE EXCEPTION 'import: % legacy row(s) already exist in another organization '
            '(imported before with a different org_id?)', foreign_ids;
    END IF;

    CREATE TEMP TABLE legacy_run_repo ON COMMIT DROP AS
    SELECT l.id AS run_id, l.repo_name AS legacy_name, r.id AS repository_id,
           r.github_installation_id, r.full_name
    FROM automaintainer_legacy.runs l
    LEFT JOIN LATERAL (
        SELECT r.* FROM repositories r
        WHERE r.org_id = org
          AND lower(r.full_name) = lower(regexp_replace(
                regexp_replace(btrim(l.repo_name), '^(https?://)?(www\.)?github\.com/', '', 'i'),
                '(\.git)?/*$', '', 'i'))
        ORDER BY r.full_name = l.repo_name DESC, r.id
        LIMIT 1
    ) r ON true;

    SELECT string_agg(DISTINCT coalesce(legacy_name, '<NULL>'), ', ')
    INTO missing
    FROM legacy_run_repo WHERE repository_id IS NULL;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'import: no repository in organization % for: % (link the GitHub App '
            'installation that covers them so they are synced, then run the import again)',
            org, missing;
    END IF;

    SELECT count(*) INTO runs_before FROM runs WHERE org_id = org;
    SELECT count(*) INTO logs_before FROM logs WHERE org_id = org;

    INSERT INTO runs (
        id, org_id, user_id, repository_id, github_installation_id, repo_name,
        target_issue_number, mode, status, error_message, github_branch_name,
        queued_at, started_at, created_at, updated_at
    )
    SELECT l.id, org, owner_id, m.repository_id, m.github_installation_id, m.full_name,
           l.target_issue,
           CASE WHEN l.target_issue IS NULL THEN 'autonomous' ELSE 'targeted_issue' END,
           CASE WHEN l.status = 'running' THEN 'failed' ELSE coalesce(l.status, 'failed') END,
           CASE WHEN l.status = 'running' OR l.status IS NULL
                THEN 'Imported from a pre-multi-tenant database while still marked running' END,
           nullif(l.branch_name, ''),
           l.created_at, l.created_at, l.created_at, l.updated_at
    FROM automaintainer_legacy.runs l
    JOIN legacy_run_repo m ON m.run_id = l.id
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS runs_added = ROW_COUNT;

    INSERT INTO logs (
        id, org_id, run_id, user_id, agent_name, log_type, message, color, metadata, created_at
    )
    SELECT l.id, org, l.run_id, NULL, l.agent_name,
           CASE WHEN l.log_type IS NULL
                  OR l.log_type IN ('message', 'ui_update', 'tool_call', 'tool_result', 'error', 'metric')
                THEN l.log_type ELSE 'message' END,
           l.message, l.color,
           CASE WHEN l.log_type IS NULL
                  OR l.log_type IN ('message', 'ui_update', 'tool_call', 'tool_result', 'error', 'metric')
                THEN l.metadata
                ELSE CASE WHEN jsonb_typeof(l.metadata) = 'object' THEN l.metadata
                          WHEN l.metadata IS NULL THEN '{}'::jsonb
                          ELSE jsonb_build_object('legacy_metadata', l.metadata) END
                     || jsonb_build_object('legacy_log_type', l.log_type) END,
           l.created_at
    FROM automaintainer_legacy.logs l
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS logs_added = ROW_COUNT;

    RAISE NOTICE 'import: inserted % run(s) and % log(s) into organization % '
        '(it had % run(s) and % log(s) before; rows already imported were skipped)',
        runs_added, logs_added, org, runs_before, logs_before;
END
$$;

-- Step 3 (after checking the history): drop the legacy tables.
DO $$
DECLARE
    left_over BIGINT;
BEGIN
    IF current_setting('automaintainer_legacy.step') <> 'drop' THEN
        RETURN;
    END IF;
    IF to_regnamespace('automaintainer_legacy') IS NULL THEN
        RAISE NOTICE 'drop: automaintainer_legacy does not exist; nothing to do';
        RETURN;
    END IF;
    SELECT (SELECT count(*) FROM automaintainer_legacy.runs l
            WHERE NOT EXISTS (SELECT 1 FROM public.runs n WHERE n.id = l.id))
         + (SELECT count(*) FROM automaintainer_legacy.logs l
            WHERE NOT EXISTS (SELECT 1 FROM public.logs n WHERE n.id = l.id))
    INTO left_over;
    IF left_over > 0 THEN
        RAISE EXCEPTION 'drop: % legacy row(s) have not been imported; run -v step=import first',
            left_over;
    END IF;
    DROP SCHEMA automaintainer_legacy CASCADE;
    RAISE NOTICE 'drop: dropped automaintainer_legacy';
END
$$;

COMMIT;
