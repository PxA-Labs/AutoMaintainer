"""Row level security of the database schema against a real PostgreSQL (#419).

Skipped unless DATABASE_URL_RLS_TEST is set, e.g.

    DATABASE_URL_RLS_TEST=postgresql://postgres:postgres@localhost:5432/postgres \\
        uv run --frozen pytest tests/rls -v

The URL must name a superuser (it creates roles and a database; CI uses the
postgres:16 service container, see .github/workflows/ci-db.yml). The suite
never touches the database in the URL: it creates a fresh, uniquely named
database on the same server, and drops it at the end.

Setup: tests/rls/supabase_shim.sql (Supabase roles, default grants, auth.uid()
and auth.users), then supabase/migrations applied by scripts/db-migrate (the
runner deployments without the Supabase CLI use, #473), then two tenants:

    org A: owner USER_A, plus USER_C with a pending (not yet joined) invite
    org B: owner USER_B

with one row per tenant table for each org. Each check runs in a transaction
as role ``authenticated`` (or ``anon``) with request.jwt.claim.sub set to the
user, exactly as PostgREST does, and is rolled back afterwards.

Tests marked ``xfail(strict=True)`` pin known defects in the schema: they
fail today, and the strict marker turns them red once the schema is fixed so
the marker is removed and the property stays enforced.
"""

import importlib.util
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

try:
    import psycopg2
    import psycopg2.errors
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn
except ImportError:  # pragma: no cover - psycopg2 is a runtime dependency
    psycopg2 = None

DATABASE_URL = os.environ.get("DATABASE_URL_RLS_TEST", "").strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set DATABASE_URL_RLS_TEST to a PostgreSQL superuser URL to run RLS tests",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIM_SQL = Path(__file__).with_name("supabase_shim.sql")

_spec = importlib.util.spec_from_file_location(
    "db_migrate", REPO_ROOT / "scripts" / "db-migrate" / "db_migrate.py"
)
db_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_migrate)
MIGRATIONS = db_migrate.discover()

USER_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
USER_C = uuid.UUID("cccccccc-0000-4000-8000-000000000003")  # pending invite to A
ORG_A = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a0")
ORG_B = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000b0")


class Tenant:
    """Fixed ids for one organisation's seed rows."""

    def __init__(self, n: int, org_id: uuid.UUID, owner: uuid.UUID):
        self.n = n
        self.org_id = org_id
        self.owner = owner
        self.installation_id = n * 1000 + 1
        self.repository_id = n * 1000 + 2
        self.run_id = uuid.UUID(int=n * 1000 + 3)
        self.login = f"tenant-{n}"


TENANT_A = Tenant(1, ORG_A, USER_A)
TENANT_B = Tenant(2, ORG_B, USER_B)

# Every table in "public", with the column that names its organisation.
# test_every_public_table_is_covered fails when a table is added without
# being listed here, so new tables cannot escape these checks.
TENANT_TABLES = {
    "organizations": "id",
    "organization_members": "org_id",
    "github_installations": "org_id",
    "repositories": "org_id",
    "runs": "org_id",
    "logs": "org_id",
    "usage_events": "org_id",
    "webhook_events": "org_id",
    "ide_sessions": "org_id",
    "verification_results": "org_id",
    "merge_decisions": "org_id",
    "repository_merge_policy_changes": "org_id",
}

# Tables clients may never write: the backend writes them with the service
# role (installations only after POST /github/installations/claim verified
# ownership with GitHub).
SERVICE_WRITTEN_TABLES = [
    "github_installations",
    "repositories",
    "logs",
    "usage_events",
    "webhook_events",
    "verification_results",
    "merge_decisions",
    "repository_merge_policy_changes",
]

# webhook_events is internal: no client may read it, even in its own org.
CLIENT_READABLE_TABLES = [t for t in TENANT_TABLES if t != "webhook_events"]


# --------------------------------------------------------------------------
# Database lifecycle
# --------------------------------------------------------------------------


def _insert_row(table: str, tenant: Tenant, user_id: uuid.UUID):
    """(INSERT statement, params) for one valid ``table`` row in ``tenant``."""
    rows = {
        "organization_members": {
            "org_id": tenant.org_id,
            "user_id": uuid.uuid4(),
            "role": "member",
            "joined_at": None,
        },
        "github_installations": {
            "id": tenant.installation_id + 100 + (user_id.int % 100),
            "org_id": tenant.org_id,
            "account_login": f"{tenant.login}-extra",
            "account_type": "Organization",
            "target_type": "Organization",
            "permissions": "{}",
            "events": "[]",
        },
        "repositories": {
            "id": tenant.repository_id + 100 + (user_id.int % 100),
            "org_id": tenant.org_id,
            "github_installation_id": tenant.installation_id,
            "full_name": f"{tenant.login}/extra-{uuid.uuid4().hex[:8]}",
            "name": "extra",
            "owner_login": tenant.login,
            "owner_type": "Organization",
        },
        "runs": {
            "id": uuid.uuid4(),
            "org_id": tenant.org_id,
            "user_id": user_id,
            "repository_id": tenant.repository_id,
            "github_installation_id": tenant.installation_id,
        },
        "logs": {
            "org_id": tenant.org_id,
            "run_id": tenant.run_id,
            "agent_name": "architect",
            "message": "forged",
        },
        "usage_events": {
            "org_id": tenant.org_id,
            "user_id": user_id,
            "run_id": tenant.run_id,
            "event_type": "agent_run_started",
        },
        "webhook_events": {
            "org_id": tenant.org_id,
            "github_installation_id": tenant.installation_id,
            "github_delivery_id": f"delivery-{uuid.uuid4()}",
            "event_type": "push",
            "payload": "{}",
        },
        "ide_sessions": {
            "org_id": tenant.org_id,
            "user_id": user_id,
            "run_id": tenant.run_id,
            "repository_id": tenant.repository_id,
        },
        "verification_results": {
            "org_id": tenant.org_id,
            "run_id": tenant.run_id,
            "repository_id": tenant.repository_id,
            "repo_name": f"{tenant.login}/repo",
            "pr_number": 1,
            "head_sha": "a" * 40,
            "status": "passed",
            "command": '["pytest"]',
        },
        "merge_decisions": {
            "org_id": tenant.org_id,
            "run_id": tenant.run_id,
            "repository_id": tenant.repository_id,
            "repo_name": f"{tenant.login}/repo",
            "pr_number": 1,
            "head_sha": "a" * 40,
            "policy": "ci",
            "decision": "merged",
            "actor": "policy",
            "reasons": '["maintainer approved", "ci checks passed"]',
            "evidence": '{"pr_url": "https://github.com/o/r/pull/1"}',
        },
        "repository_merge_policy_changes": {
            "org_id": tenant.org_id,
            "repository_id": tenant.repository_id,
            "old_policy": None,
            "new_policy": "human",
            "changed_by": user_id,
        },
    }
    row = rows[table]
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, row)),
        sql.SQL(", ").join(sql.Placeholder() * len(row)),
    )
    return statement, [str(v) if isinstance(v, uuid.UUID) else v for v in row.values()]


def _set_user(cur, user_id):
    cur.execute(
        "SELECT set_config('request.jwt.claim.sub', %s, true)",
        (str(user_id) if user_id else "",),
    )


def _seed(conn):
    with conn.cursor() as cur:
        for user_id, email in (
            (USER_A, "a@example.test"),
            (USER_B, "b@example.test"),
            (USER_C, "c@example.test"),
        ):
            cur.execute(
                "INSERT INTO auth.users (id, email, raw_user_meta_data) VALUES (%s, %s, %s)",
                (str(user_id), email, '{"full_name": "%s"}' % email.split("@")[0]),
            )
        for tenant in (TENANT_A, TENANT_B):
            # on_org_created makes auth.uid() the owner, as when a signed-in
            # user creates an organisation.
            _set_user(cur, tenant.owner)
            cur.execute(
                "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
                (str(tenant.org_id), tenant.login, tenant.login),
            )
            _set_user(cur, None)
            cur.execute(
                "INSERT INTO github_installations (id, org_id, account_login, account_type,"
                " target_type, permissions, events) VALUES (%s, %s, %s, 'Organization',"
                " 'Organization', '{}', '[]')",
                (tenant.installation_id, str(tenant.org_id), tenant.login),
            )
            cur.execute(
                "INSERT INTO repositories (id, org_id, github_installation_id, full_name,"
                " name, owner_login, owner_type) VALUES (%s, %s, %s, %s, 'repo', %s,"
                " 'Organization')",
                (
                    tenant.repository_id,
                    str(tenant.org_id),
                    tenant.installation_id,
                    f"{tenant.login}/repo",
                    tenant.login,
                ),
            )
            cur.execute(
                "INSERT INTO runs (id, org_id, user_id, repository_id, github_installation_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    str(tenant.run_id),
                    str(tenant.org_id),
                    str(tenant.owner),
                    tenant.repository_id,
                    tenant.installation_id,
                ),
            )
            for table in (
                "logs",
                "usage_events",
                "webhook_events",
                "ide_sessions",
                "verification_results",
                "merge_decisions",
                "repository_merge_policy_changes",
            ):
                cur.execute(*_insert_row(table, tenant, tenant.owner))
        # A pending invite: a membership row that has not been accepted yet.
        cur.execute(
            "INSERT INTO organization_members (org_id, user_id, role, invited_by)"
            " VALUES (%s, %s, 'member', %s)",
            (str(ORG_A), str(USER_C), str(USER_A)),
        )
    conn.commit()


@pytest.fixture(scope="module")
def db():
    if psycopg2 is None:
        pytest.fail(
            "psycopg2 is required for the RLS tests (uv sync --frozen --group dev)"
        )

    admin = psycopg2.connect(DATABASE_URL)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cur.fetchone()[0]:
            admin.close()
            pytest.fail("DATABASE_URL_RLS_TEST must connect as a superuser")
        name = f"automaintainer_rls_{uuid.uuid4().hex[:12]}"
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    conn = None
    try:
        conn = psycopg2.connect(make_dsn(DATABASE_URL, dbname=name))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public'"
            )
            assert cur.fetchone()[0] == 0, "expected an empty database"
            cur.execute(SHIM_SQL.read_text(encoding="utf-8"))
        conn.commit()
        db_migrate.up(conn, MIGRATIONS, log=lambda *_: None)
        _seed(conn)
        yield conn
    finally:
        if conn is not None:
            conn.close()
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
        admin.close()


@contextmanager
def session(conn, role, user_id=None):
    """A cursor acting as ``role`` with auth.uid() = ``user_id``; rolled back."""
    assert role in ("anon", "authenticated", "service_role")
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            _set_user(cur, user_id)
            yield cur
    finally:
        conn.rollback()


def attempt(cur, statement, params=()):
    """Run one write; return its rowcount, or "denied" on an RLS/privilege error."""
    cur.execute("SAVEPOINT attempt")
    try:
        cur.execute(statement, params)
    except psycopg2.errors.InsufficientPrivilege:
        cur.execute("ROLLBACK TO SAVEPOINT attempt")
        return "denied"
    # Read the count before RELEASE: rowcount reflects the last command run
    # on the cursor, and RELEASE SAVEPOINT reports -1.
    rowcount = cur.rowcount
    cur.execute("RELEASE SAVEPOINT attempt")
    return rowcount


def visible_orgs(cur, table):
    cur.execute(
        sql.SQL("SELECT DISTINCT {}::text FROM {}").format(
            sql.Identifier(TENANT_TABLES[table]), sql.Identifier(table)
        )
    )
    return {uuid.UUID(row[0]) for row in cur.fetchall()}


def row_count(cur, table):
    cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
    return cur.fetchone()[0]


def _touch(table, tenant):
    """UPDATE that changes nothing, aimed at ``tenant``'s rows."""
    column = sql.Identifier(TENANT_TABLES[table])
    return (
        sql.SQL("UPDATE {t} SET {c} = {c} WHERE {c} = %s").format(
            t=sql.Identifier(table), c=column
        ),
        (str(tenant.org_id),),
    )


def _delete(table, tenant):
    return (
        sql.SQL("DELETE FROM {} WHERE {} = %s").format(
            sql.Identifier(table), sql.Identifier(TENANT_TABLES[table])
        ),
        (str(tenant.org_id),),
    )


# --------------------------------------------------------------------------
# Schema-wide properties
# --------------------------------------------------------------------------


def test_every_public_table_is_covered(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        )
        tables = {row[0] for row in cur.fetchall()}
    db.rollback()
    assert tables == set(TENANT_TABLES), (
        "Update TENANT_TABLES (and the per-table checks) for: "
        f"added {sorted(tables - set(TENANT_TABLES))}, removed {sorted(set(TENANT_TABLES) - tables)}"
    )


def test_rls_is_enabled_on_every_public_table(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity"
            " ORDER BY 1"
        )
        unprotected = [row[0] for row in cur.fetchall()]
    db.rollback()
    assert not unprotected, f"RLS is not enabled on: {unprotected}"


def test_every_public_view_uses_security_invoker(db):
    """A view without security_invoker runs as its owner and bypasses RLS."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = 'v'"
            " AND NOT coalesce('security_invoker=true' = ANY(c.reloptions), false)"
            " ORDER BY 1"
        )
        leaky = [row[0] for row in cur.fetchall()]
    db.rollback()
    assert not leaky, f"views without security_invoker: {leaky}"


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(TENANT_TABLES))
def test_members_read_only_their_own_org(db, table):
    for tenant in (TENANT_A, TENANT_B):
        expected = {tenant.org_id} if table in CLIENT_READABLE_TABLES else set()
        with session(db, "authenticated", tenant.owner) as cur:
            assert (
                visible_orgs(cur, table) == expected
            ), f"{table} as owner of tenant {tenant.n}"


@pytest.mark.parametrize(
    "view", ["monthly_usage_summary", "org_health", "recent_runs_detailed"]
)
def test_views_show_only_own_org(db, view):
    with session(db, "authenticated", USER_A) as cur:
        cur.execute(
            sql.SQL("SELECT DISTINCT org_id::text FROM {}").format(sql.Identifier(view))
        )
        assert {uuid.UUID(r[0]) for r in cur.fetchall()} == {ORG_A}


@pytest.mark.parametrize("table", sorted(TENANT_TABLES))
def test_pending_invite_grants_no_access(db, table):
    with session(db, "authenticated", USER_C) as cur:
        assert row_count(cur, table) == 0


def test_pending_invitee_cannot_accept_own_invite_or_start_runs(db):
    with session(db, "authenticated", USER_C) as cur:
        accepted = attempt(
            cur,
            "UPDATE organization_members SET joined_at = now() WHERE user_id = %s",
            (str(USER_C),),
        )
        assert accepted == 0
        assert attempt(cur, *_insert_row("runs", TENANT_A, USER_C)) == "denied"


@pytest.mark.parametrize("table", sorted(TENANT_TABLES))
def test_anon_sees_nothing(db, table):
    with session(db, "anon") as cur:
        assert row_count(cur, table) == 0
    # A forged user id without an authenticated JWT role grants nothing.
    with session(db, "anon", USER_A) as cur:
        assert row_count(cur, table) == 0


@pytest.mark.parametrize("table", sorted(TENANT_TABLES))
def test_anon_cannot_write(db, table):
    with session(db, "anon", USER_A) as cur:
        assert attempt(cur, *_touch(table, TENANT_A)) == 0
        assert attempt(cur, *_delete(table, TENANT_A)) == 0
        if table != "organizations":
            assert attempt(cur, *_insert_row(table, TENANT_A, USER_A)) == "denied"


# --------------------------------------------------------------------------
# Cross-tenant writes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(TENANT_TABLES))
def test_cannot_update_or_delete_another_orgs_rows(db, table):
    with session(db, "authenticated", USER_A) as cur:
        assert attempt(cur, *_touch(table, TENANT_B)) == 0
        assert attempt(cur, *_delete(table, TENANT_B)) == 0
        cur.execute("RESET ROLE")
        cur.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(TENANT_TABLES[table])
            ),
            (str(ORG_B),),
        )
        assert cur.fetchone()[0] > 0, "org B's rows must survive"


@pytest.mark.parametrize("table", sorted(set(TENANT_TABLES) - {"organizations"}))
def test_cannot_insert_into_another_org(db, table):
    with session(db, "authenticated", USER_A) as cur:
        assert attempt(cur, *_insert_row(table, TENANT_B, USER_A)) == "denied"


@pytest.mark.parametrize("table", SERVICE_WRITTEN_TABLES)
def test_service_written_tables_reject_client_writes_in_own_org(db, table):
    with session(db, "authenticated", USER_A) as cur:
        assert attempt(cur, *_insert_row(table, TENANT_A, USER_A)) == "denied"
        assert attempt(cur, *_touch(table, TENANT_A)) == 0
        assert attempt(cur, *_delete(table, TENANT_A)) == 0


@pytest.mark.parametrize("table", SERVICE_WRITTEN_TABLES)
def test_service_role_can_write_service_tables(db, table):
    with session(db, "service_role") as cur:
        assert attempt(cur, *_insert_row(table, TENANT_A, USER_A)) == 1


def test_runs_cannot_be_moved_to_another_org(db):
    with session(db, "authenticated", USER_A) as cur:
        moved = attempt(
            cur,
            "UPDATE runs SET org_id = %s WHERE id = %s",
            (str(ORG_B), str(TENANT_A.run_id)),
        )
        assert moved == "denied"


# --------------------------------------------------------------------------
# Own-org writes the policies allow
# --------------------------------------------------------------------------


def test_owner_can_update_own_org(db):
    with session(db, "authenticated", USER_A) as cur:
        assert (
            attempt(
                cur,
                "UPDATE organizations SET name = 'renamed' WHERE id = %s",
                (str(ORG_A),),
            )
            == 1
        )


def test_owner_can_invite_into_own_org(db):
    with session(db, "authenticated", USER_A) as cur:
        assert attempt(cur, *_insert_row("organization_members", TENANT_A, USER_A)) == 1


def test_member_can_start_and_update_own_runs(db):
    with session(db, "authenticated", USER_A) as cur:
        statement, params = _insert_row("runs", TENANT_A, USER_A)
        assert attempt(cur, statement, params) == 1
        new_run = params[0]
        assert (
            attempt(
                cur, "UPDATE runs SET status = 'cancelled' WHERE id = %s", (new_run,)
            )
            == 1
        )
        cur.execute("SELECT org_id::text FROM runs WHERE id = %s", (new_run,))
        assert cur.fetchone() == (str(ORG_A),)


def test_member_cannot_start_runs_as_someone_else(db):
    with session(db, "authenticated", USER_A) as cur:
        assert attempt(cur, *_insert_row("runs", TENANT_A, USER_B)) == "denied"


def test_member_can_open_and_update_own_ide_session(db):
    with session(db, "authenticated", USER_A) as cur:
        assert attempt(cur, *_insert_row("ide_sessions", TENANT_A, USER_A)) == 1
        updated = attempt(
            cur,
            "UPDATE ide_sessions SET status = 'idle' WHERE user_id = %s",
            (str(USER_A),),
        )
        assert updated >= 1


def test_creating_an_org_makes_the_creator_its_owner(db):
    new_org = uuid.uuid4()
    with session(db, "authenticated", USER_B) as cur:
        created = attempt(
            cur,
            "INSERT INTO organizations (id, name, slug) VALUES (%s, 'new', %s)",
            (str(new_org), f"new-{new_org.hex[:8]}"),
        )
        assert created == 1
        cur.execute(
            "SELECT role FROM organization_members WHERE org_id = %s AND user_id = %s",
            (str(new_org), str(USER_B)),
        )
        assert cur.fetchone() == ("owner",)
        assert new_org in visible_orgs(cur, "organizations")


# --------------------------------------------------------------------------
# Migrations (#473; more in test_migrations.py)
# --------------------------------------------------------------------------


def test_schema_can_be_reapplied_without_losing_data(db):
    def counts(cur):
        return {table: row_count(cur, table) for table in sorted(TENANT_TABLES)}

    with db.cursor() as cur:
        before = counts(cur)
    db.rollback()
    try:
        # The runner skips what it recorded ...
        assert db_migrate.up(db, MIGRATIONS, log=lambda *_: None) == []
        # ... and every file is re-runnable even when applied again
        # regardless (a pasted supabase_schema.sql, an untracked database).
        with db.cursor() as cur:
            for migration in MIGRATIONS:
                cur.execute(migration.sql)
            assert counts(cur) == before
            assert before["runs"] >= 2 and before["logs"] >= 2
    finally:
        db.rollback()


def test_runs_has_the_columns_the_backend_uses(db):
    # Columns written or read through the Supabase client: run_data in
    # main.start_agents(), update_run_status() calls and the stale-run reaper
    # in tasks.py, the upsert and checkpoints in agents.py.
    used = {
        "id",
        "org_id",
        "user_id",
        "repository_id",
        "github_installation_id",
        "repo_name",
        "target_issue_number",
        "mode",
        "status",
        "queued_at",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
        "celery_task_id",
        "error_message",
        "result_summary",
        "github_issue_number",
        "checkpoint",
    }
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'runs'"
        )
        columns = {row[0] for row in cur.fetchall()}
    db.rollback()
    assert used <= columns, f"missing columns: {sorted(used - columns)}"


def test_run_cannot_reference_another_orgs_repository(db):
    with session(db, "authenticated", USER_A) as cur:
        result = attempt(
            cur,
            "INSERT INTO runs (org_id, user_id, repository_id, github_installation_id)"
            " VALUES (%s, %s, %s, %s)",
            (str(ORG_A), str(USER_A), TENANT_B.repository_id, TENANT_B.installation_id),
        )
        assert result == "denied"


def test_ide_session_cannot_be_moved_to_another_org(db):
    with session(db, "authenticated", USER_A) as cur:
        moved = attempt(
            cur,
            "UPDATE ide_sessions SET org_id = %s WHERE user_id = %s",
            (str(ORG_B), str(USER_A)),
        )
        assert moved == "denied"


# --------------------------------------------------------------------------
# verification_results (epic #411)
# --------------------------------------------------------------------------


def test_verification_rows_are_read_by_members_only(db):
    with session(db, "authenticated", USER_A) as cur:
        cur.execute(
            "SELECT DISTINCT run_id::text FROM verification_results WHERE org_id = %s",
            (str(ORG_A),),
        )
        assert cur.fetchall() == [(str(TENANT_A.run_id),)]
    with session(db, "authenticated", USER_B) as cur:
        cur.execute(
            "SELECT count(*) FROM verification_results WHERE org_id = %s",
            (str(ORG_A),),
        )
        assert cur.fetchone() == (0,)


def test_members_cannot_forge_or_rewrite_verification_rows(db):
    with session(db, "authenticated", USER_A) as cur:
        statement, params = _insert_row("verification_results", TENANT_A, USER_A)
        assert attempt(cur, statement, params) == "denied"
        rewritten = attempt(
            cur,
            "UPDATE verification_results SET status = 'passed' WHERE org_id = %s",
            (str(ORG_A),),
        )
        assert rewritten == 0
        assert attempt(cur, *_delete("verification_results", TENANT_A)) == 0


@pytest.mark.parametrize(
    "column, value",
    [
        ("status", "maybe"),
        ("output_tail", "x" * 4001),
        ("summary", "x" * 1001),
        ("command", '"pytest -q"'),
    ],
    ids=["status", "output_tail", "summary", "command"],
)
def test_verification_rows_reject_invalid_values(db, column, value):
    row = {
        "org_id": str(ORG_A),
        "run_id": str(TENANT_A.run_id),
        "head_sha": "a" * 40,
        "status": "passed",
        column: value,
    }
    statement = sql.SQL("INSERT INTO verification_results ({}) VALUES ({})").format(
        sql.SQL(", ").join(map(sql.Identifier, row)),
        sql.SQL(", ").join(sql.Placeholder() * len(row)),
    )
    with session(db, "service_role") as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(statement, list(row.values()))


def test_verification_rows_go_with_their_run(db):
    with session(db, "service_role") as cur:
        cur.execute("DELETE FROM runs WHERE id = %s", (str(TENANT_A.run_id),))
        cur.execute(
            "SELECT count(*) FROM verification_results WHERE run_id = %s",
            (str(TENANT_A.run_id),),
        )
        assert cur.fetchone() == (0,)


def test_verification_results_are_not_published_to_realtime(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_publication_tables"
            " WHERE pubname = 'supabase_realtime' AND tablename = 'verification_results'"
        )
        assert cur.fetchone() == (0,)
    db.rollback()


# --------------------------------------------------------------------------
# merge_decisions (epic #411)
# --------------------------------------------------------------------------


def test_merge_decisions_are_read_by_members_only(db):
    with session(db, "authenticated", USER_A) as cur:
        cur.execute(
            "SELECT DISTINCT run_id::text FROM merge_decisions WHERE org_id = %s",
            (str(ORG_A),),
        )
        assert cur.fetchall() == [(str(TENANT_A.run_id),)]
    for user in (USER_B, USER_C):  # another org; a pending invite to org A
        with session(db, "authenticated", user) as cur:
            cur.execute(
                "SELECT count(*) FROM merge_decisions WHERE org_id = %s",
                (str(ORG_A),),
            )
            assert cur.fetchone() == (0,)


def test_members_cannot_forge_or_rewrite_merge_decisions(db):
    with session(db, "authenticated", USER_A) as cur:
        statement, params = _insert_row("merge_decisions", TENANT_A, USER_A)
        assert attempt(cur, statement, params) == "denied"
        rewritten = attempt(
            cur,
            "UPDATE merge_decisions SET decision = 'merged' WHERE org_id = %s",
            (str(ORG_A),),
        )
        assert rewritten == 0
        assert attempt(cur, *_delete("merge_decisions", TENANT_A)) == 0


def test_service_role_writes_merge_decisions(db):
    with session(db, "service_role") as cur:
        cur.execute(*_insert_row("merge_decisions", TENANT_A, USER_A))
        cur.execute(
            "SELECT count(*) FROM merge_decisions WHERE run_id = %s",
            (str(TENANT_A.run_id),),
        )
        assert cur.fetchone() == (2,)


@pytest.mark.parametrize(
    "column, value",
    [
        ("policy", "yolo"),
        ("decision", "approved"),
        ("actor", "robot"),
        ("actor_role", "x" * 101),
        ("reasons", '"maintainer rejected"'),
        ("reasons", "[" + ", ".join(['"r"'] * 21) + "]"),
        ("reasons", '["' + "x" * 4000 + '"]'),
        ("evidence", '["https://github.com/o/r/pull/1"]'),
        ("evidence", '{"pr_url": "' + "x" * 4000 + '"}'),
    ],
    ids=[
        "policy",
        "decision",
        "actor",
        "actor_role",
        "reasons-not-array",
        "reasons-count",
        "reasons-size",
        "evidence-not-object",
        "evidence-size",
    ],
)
def test_merge_decisions_reject_invalid_values(db, column, value):
    row = {
        "org_id": str(ORG_A),
        "run_id": str(TENANT_A.run_id),
        "head_sha": "a" * 40,
        "policy": "human",
        "decision": "held",
        "actor": "policy",
        column: value,
    }
    statement = sql.SQL("INSERT INTO merge_decisions ({}) VALUES ({})").format(
        sql.SQL(", ").join(map(sql.Identifier, row)),
        sql.SQL(", ").join(sql.Placeholder() * len(row)),
    )
    with session(db, "service_role") as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(statement, list(row.values()))


def test_merge_decisions_go_with_their_run(db):
    with session(db, "service_role") as cur:
        cur.execute("DELETE FROM runs WHERE id = %s", (str(TENANT_A.run_id),))
        cur.execute(
            "SELECT count(*) FROM merge_decisions WHERE run_id = %s",
            (str(TENANT_A.run_id),),
        )
        assert cur.fetchone() == (0,)


def test_merge_decisions_are_not_published_to_realtime(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_publication_tables"
            " WHERE pubname = 'supabase_realtime' AND tablename = 'merge_decisions'"
        )
        assert cur.fetchone() == (0,)
    db.rollback()


# --------------------------------------------------------------------------
# repositories.merge_policy (epic #411, backend/merge_policy.py)
# --------------------------------------------------------------------------


def _set_merge_policy(value, tenant=TENANT_A):
    return (
        "UPDATE repositories SET merge_policy = %s WHERE id = %s",
        (value, tenant.repository_id),
    )


def _merge_policy_of(cur, tenant=TENANT_A):
    cur.execute(
        "SELECT merge_policy FROM repositories WHERE id = %s",
        (tenant.repository_id,),
    )
    return cur.fetchall()


def test_members_read_their_repositories_merge_policy(db):
    with session(db, "service_role") as cur:
        cur.execute(*_set_merge_policy("ci"))
        cur.execute("SET LOCAL ROLE authenticated")
        _set_user(cur, USER_A)
        assert _merge_policy_of(cur) == [("ci",)]
        for user in (USER_B, USER_C):  # another org; a pending invite to org A
            _set_user(cur, user)
            assert _merge_policy_of(cur) == []


def test_org_owner_cannot_write_merge_policy_directly(db):
    """Only the backend writes it (PUT /repositories/{id}/merge-policy checks
    the org role); PostgREST requests as authenticated change nothing."""
    with session(db, "authenticated", USER_A) as cur:
        assert attempt(cur, *_set_merge_policy("auto")) == 0
        assert attempt(cur, *_set_merge_policy("auto", TENANT_B)) == 0
        cur.execute("RESET ROLE")
        assert _merge_policy_of(cur) == [(None,)]
        assert _merge_policy_of(cur, TENANT_B) == [(None,)]


def test_merge_policy_stays_service_only_if_an_update_policy_is_added(db):
    """Defense in depth: a future client UPDATE policy on repositories (for
    another column) must not make merge_policy client-writable."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "CREATE POLICY repos_update_test ON repositories FOR UPDATE"
                " TO authenticated USING (true) WITH CHECK (true)"
            )
            cur.execute("SET LOCAL ROLE authenticated")
            _set_user(cur, USER_A)
            assert attempt(cur, *_set_merge_policy("human")) == "denied"
            # The trigger guards merge_policy only.
            touched = attempt(
                cur,
                "UPDATE repositories SET description = 'x' WHERE id = %s",
                (TENANT_A.repository_id,),
            )
            assert touched == 1
    finally:
        db.rollback()


def test_service_role_sets_and_clears_merge_policy(db):
    with session(db, "service_role") as cur:
        for value in ("human", "ci", "auto", None):
            assert attempt(cur, *_set_merge_policy(value)) == 1
            assert _merge_policy_of(cur) == [(value,)]


@pytest.mark.parametrize("value", ["yolo", "HUMAN", ""])
def test_merge_policy_rejects_unknown_values(db, value):
    with session(db, "service_role") as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(*_set_merge_policy(value))


def test_repository_sync_upsert_keeps_merge_policy(db):
    """github_app.sync_installation_repositories upserts rows without the
    column; an existing setting must survive a re-sync."""
    with session(db, "service_role") as cur:
        cur.execute(*_set_merge_policy("human"))
        cur.execute(
            "INSERT INTO repositories (id, org_id, github_installation_id, full_name,"
            " name, owner_login, owner_type) VALUES (%s, %s, %s, %s, 'repo', %s,"
            " 'Organization') ON CONFLICT (id) DO UPDATE SET"
            " full_name = EXCLUDED.full_name, synced_at = now()",
            (
                TENANT_A.repository_id,
                str(ORG_A),
                TENANT_A.installation_id,
                f"{TENANT_A.login}/repo",
                TENANT_A.login,
            ),
        )
        assert _merge_policy_of(cur) == [("human",)]


# --------------------------------------------------------------------------
# repository_merge_policy_changes (epic #411)
# --------------------------------------------------------------------------

SET_POLICY = "SELECT set_repository_merge_policy(%s, %s, %s, %s)"


def _set_policy_args(value, tenant=TENANT_A, actor=USER_A, org=None):
    return (tenant.repository_id, str(org or tenant.org_id), value, str(actor))


def _policy_changes(cur, tenant=TENANT_A):
    cur.execute(
        "SELECT old_policy, new_policy, changed_by::text FROM"
        " repository_merge_policy_changes WHERE repository_id = %s"
        " ORDER BY changed_at, id",
        (tenant.repository_id,),
    )
    return cur.fetchall()


def test_policy_changes_are_read_by_members_only(db):
    with session(db, "authenticated", USER_A) as cur:
        assert _policy_changes(cur) == [(None, "human", str(USER_A))]
        assert _policy_changes(cur, TENANT_B) == []
    for user in (USER_B, USER_C):  # another org; a pending invite to org A
        with session(db, "authenticated", user) as cur:
            assert _policy_changes(cur) == []


def test_members_cannot_forge_or_rewrite_policy_changes(db):
    with session(db, "authenticated", USER_A) as cur:
        statement, params = _insert_row(
            "repository_merge_policy_changes", TENANT_A, USER_A
        )
        assert attempt(cur, statement, params) == "denied"
        rewritten = attempt(
            cur,
            "UPDATE repository_merge_policy_changes SET changed_by = %s",
            (str(USER_B),),
        )
        assert rewritten == 0
        assert attempt(cur, *_delete("repository_merge_policy_changes", TENANT_A)) == 0


def test_policy_changes_are_append_only_even_for_the_service_role(db):
    with session(db, "service_role") as cur:
        rewritten = attempt(
            cur,
            "UPDATE repository_merge_policy_changes SET new_policy = 'auto'"
            " WHERE org_id = %s",
            (str(ORG_A),),
        )
        assert rewritten == "denied"


def test_set_policy_updates_and_records_the_actor_atomically(db):
    with session(db, "service_role") as cur:
        cur.execute(SET_POLICY, _set_policy_args("ci"))
        (change,) = cur.fetchone()
        assert (change["old_policy"], change["new_policy"]) == (None, "ci")
        assert change["changed_by"] == str(USER_A)
        assert _merge_policy_of(cur) == [("ci",)]
        cur.execute(SET_POLICY, _set_policy_args(None, actor=USER_C))
        # One transaction: now() is the same for both rows, so compare as a
        # set (the seed row plus the two changes).
        assert sorted(_policy_changes(cur), key=repr) == sorted(
            [
                (None, "human", str(USER_A)),
                (None, "ci", str(USER_A)),
                ("ci", None, str(USER_C)),
            ],
            key=repr,
        )
        assert _merge_policy_of(cur) == [(None,)]


@pytest.mark.parametrize("value", [None, "human"])
def test_set_policy_to_the_current_value_writes_no_row(db, value):
    with session(db, "service_role") as cur:
        cur.execute(*_set_merge_policy(value))
        before = _policy_changes(cur)
        cur.execute(SET_POLICY, _set_policy_args(value))
        assert cur.fetchone() == (None,)
        assert _policy_changes(cur) == before


def test_set_policy_in_another_org_is_not_found(db):
    with session(db, "service_role") as cur:
        cur.execute("SAVEPOINT change")
        with pytest.raises(psycopg2.errors.NoDataFound):
            cur.execute(SET_POLICY, _set_policy_args("human", TENANT_B, org=ORG_A))
        cur.execute("ROLLBACK TO SAVEPOINT change")
        assert _merge_policy_of(cur, TENANT_B) == [(None,)]
        assert _policy_changes(cur, TENANT_B) == [(None, "human", str(USER_B))]  # seed


def test_set_policy_requires_an_actor(db):
    with session(db, "service_role") as cur:
        with pytest.raises(psycopg2.errors.NullValueNotAllowed):
            cur.execute(SET_POLICY, (TENANT_A.repository_id, str(ORG_A), "ci", None))


def test_an_audit_failure_leaves_the_policy_unchanged(db):
    with session(db, "service_role") as cur:
        # Make the audit insert fail (created in this transaction, so the
        # rollback at the end of the session removes it again).
        cur.execute("RESET ROLE")
        cur.execute(
            "CREATE FUNCTION rls_test_fail_audit() RETURNS trigger LANGUAGE plpgsql"
            " AS $$ BEGIN RAISE EXCEPTION 'audit unavailable'; END; $$"
        )
        cur.execute(
            "CREATE TRIGGER rls_test_fail_audit"
            " BEFORE INSERT ON repository_merge_policy_changes"
            " FOR EACH ROW EXECUTE FUNCTION rls_test_fail_audit()"
        )
        cur.execute("SET LOCAL ROLE service_role")
        cur.execute("SAVEPOINT change")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute(SET_POLICY, _set_policy_args("human"))
        cur.execute("ROLLBACK TO SAVEPOINT change")
        assert _merge_policy_of(cur) == [(None,)]
        assert _policy_changes(cur) == [(None, "human", str(USER_A))]  # seed only


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_clients_cannot_call_set_policy(db, role):
    with session(db, role, USER_A) as cur:
        assert attempt(cur, SET_POLICY, _set_policy_args("human")) == "denied"
        cur.execute("RESET ROLE")
        assert _merge_policy_of(cur) == [(None,)]
