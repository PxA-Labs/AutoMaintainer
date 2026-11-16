"""supabase/migrations and scripts/db-migrate against a real PostgreSQL (#473).

Skipped unless DATABASE_URL_RLS_TEST is set; see test_rls_policies.py for the
URL and .github/workflows/ci-db.yml for the CI job that runs it.

Each test works on its own freshly created database (Supabase shim, then the
migrations through the runner, then one tenant's rows) and drops it at the
end. They check the property every migration must keep: applying the
migrations again -- through the runner, file by file, or as the generated
supabase_schema.sql -- changes nothing and keeps every row, and applying them
to a database built from an older single-file schema brings it up to date.
"""

import importlib.util
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

try:
    import psycopg2
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
SCHEMA_SQL = REPO_ROOT / "supabase_schema.sql"

_spec = importlib.util.spec_from_file_location(
    "db_migrate", REPO_ROOT / "scripts" / "db-migrate" / "db_migrate.py"
)
db_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_migrate)
MIGRATIONS = db_migrate.discover()

ORG = "aaaaaaaa-0000-4000-8000-0000000000a0"
USER = "aaaaaaaa-0000-4000-8000-000000000001"
RUN = "00000000-0000-4000-8000-000000000003"

# Catalog queries whose results must not change when migrations are re-run.
CATALOG = {
    "columns": "SELECT table_name, column_name, data_type, is_nullable, column_default"
    " FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2",
    "rls": "SELECT c.relname, c.relrowsecurity FROM pg_class c"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1",
    "policies": "SELECT tablename, policyname, cmd, roles::text, qual, with_check"
    " FROM pg_policies WHERE schemaname = 'public' ORDER BY 1, 2",
    "functions": "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p"
    " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
    " ORDER BY 1",
    "triggers": "SELECT t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t"
    " JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND NOT t.tgisinternal ORDER BY 1",
    "indexes": "SELECT indexname, indexdef FROM pg_indexes"
    " WHERE schemaname = 'public' ORDER BY 1",
    "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)"
    " FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2",
    "views": "SELECT c.relname, c.reloptions::text, pg_get_viewdef(c.oid) FROM pg_class c"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relkind = 'v' ORDER BY 1",
    "publication": "SELECT tablename FROM pg_publication_tables"
    " WHERE pubname = 'supabase_realtime' ORDER BY 1",
}

DATA_TABLES = ["organizations", "organization_members", "runs", "logs"]


def _quiet(*_):
    pass


@contextmanager
def fresh_database(*, migrate=True):
    """A connection to a new database with the shim (and the migrations)."""
    if psycopg2 is None:
        pytest.fail(
            "psycopg2 is required for the RLS tests (uv sync --frozen --group dev)"
        )
    admin = psycopg2.connect(DATABASE_URL)
    admin.autocommit = True
    name = f"automaintainer_mig_{uuid.uuid4().hex[:12]}"
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    conn = None
    try:
        conn = psycopg2.connect(make_dsn(DATABASE_URL, dbname=name))
        with conn.cursor() as cur:
            cur.execute(SHIM_SQL.read_text(encoding="utf-8"))
        conn.commit()
        if migrate:
            db_migrate.up(conn, MIGRATIONS, log=_quiet)
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


def seed(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auth.users (id, email) VALUES (%s, 'a@example.test')", (USER,)
        )
        cur.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (USER,))
        cur.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s, 'a', 'a')", (ORG,)
        )
        cur.execute(
            "INSERT INTO github_installations (id, org_id, account_login, account_type,"
            " target_type, permissions, events) VALUES (1001, %s, 'a', 'Organization',"
            " 'Organization', '{}', '[]')",
            (ORG,),
        )
        cur.execute(
            "INSERT INTO repositories (id, org_id, github_installation_id, full_name,"
            " name, owner_login, owner_type) VALUES (1002, %s, 1001, 'a/repo', 'repo',"
            " 'a', 'Organization')",
            (ORG,),
        )
        cur.execute(
            "INSERT INTO runs (id, org_id, user_id, repository_id, github_installation_id,"
            " repo_name, checkpoint) VALUES (%s, %s, %s, 1002, 1001, 'a/repo',"
            " '{\"issue_number\": 7}')",
            (RUN, ORG, USER),
        )
        cur.execute(
            "INSERT INTO logs (org_id, run_id, agent_name, message)"
            " VALUES (%s, %s, 'architect', 'hello')",
            (ORG, RUN),
        )
    conn.commit()


def catalog(cur):
    result = {}
    for key, query in CATALOG.items():
        cur.execute(query)
        result[key] = cur.fetchall()
    return result


def data(cur):
    result = {}
    for table in DATA_TABLES:
        cur.execute(
            sql.SQL("SELECT to_jsonb(t)::text FROM {} t ORDER BY 1").format(
                sql.Identifier(table)
            )
        )
        result[table] = [row[0] for row in cur.fetchall()]
    return result


@pytest.fixture
def db():
    with fresh_database() as conn:
        seed(conn)
        yield conn


def test_runner_records_every_migration_and_skips_them_next_time(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT version, checksum, baselined"
            " FROM automaintainer_migrations.schema_migrations ORDER BY 1"
        )
        assert cur.fetchall() == [(m.version, m.checksum, False) for m in MIGRATIONS]
    db.rollback()
    assert db_migrate.up(db, MIGRATIONS, log=_quiet) == []
    assert db_migrate.status(db, MIGRATIONS, log=_quiet) == []


def test_tracking_table_is_not_in_the_exposed_schema(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT table_schema FROM information_schema.tables"
            " WHERE table_name = 'schema_migrations'"
        )
        assert cur.fetchall() == [("automaintainer_migrations",)]
    db.rollback()


@pytest.mark.parametrize("source", ["migration files", "supabase_schema.sql"])
def test_reapplying_changes_nothing_and_keeps_data(db, source):
    with db.cursor() as cur:
        before = catalog(cur), data(cur)
        if source == "migration files":
            for migration in MIGRATIONS:
                cur.execute(migration.sql)
        else:
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        db.commit()
        assert (catalog(cur), data(cur)) == before
    db.rollback()


def test_upgrades_a_database_built_from_an_older_schema(db):
    """Simulate a deployment that never ran the hand-picked upgrade notes."""
    with db.cursor() as cur:
        expected_catalog = catalog(cur)
        expected_data = data(cur)
        # Undo, in place, what #452, #466, #472 and the security_invoker views
        # added to the old single-file schema. Seeded checkpoint/repo_name
        # values go with their columns; every other row stays.
        cur.execute("""
            DROP VIEW recent_runs_detailed;
            ALTER TABLE runs DROP COLUMN checkpoint, DROP COLUMN repo_name;
            DROP POLICY runs_insert_own ON runs;
            DROP POLICY runs_update_own ON runs;
            DROP POLICY ide_insert_own ON ide_sessions;
            DROP POLICY ide_update_own ON ide_sessions;
            DROP FUNCTION repository_in_org(BIGINT, UUID, BIGINT);
            DROP FUNCTION run_in_org(UUID, UUID);
            DROP TRIGGER repositories_org_immutable ON repositories;
            DROP FUNCTION prevent_repository_org_change();
            DROP POLICY github_install_insert_service ON github_installations;
            DROP POLICY github_install_update_service ON github_installations;
            CREATE POLICY github_install_insert_admin ON github_installations
                FOR INSERT TO authenticated WITH CHECK (user_is_org_admin(org_id));
            CREATE POLICY github_install_update_admin ON github_installations
                FOR UPDATE TO authenticated USING (user_is_org_admin(org_id));
            CREATE POLICY runs_insert_own ON runs FOR INSERT TO authenticated
                WITH CHECK (org_id = ANY(current_user_org_ids()) AND user_id = auth.uid());
            CREATE POLICY runs_update_own ON runs FOR UPDATE TO authenticated
                USING (org_id = ANY(current_user_org_ids()) AND user_id = auth.uid());
            DROP VIEW monthly_usage_summary;
            CREATE VIEW monthly_usage_summary AS SELECT org_id FROM usage_events;
            ALTER PUBLICATION supabase_realtime DROP TABLE ide_sessions;
            DROP SCHEMA automaintainer_migrations CASCADE;
            """)
        db.commit()

        assert db_migrate.up(db, MIGRATIONS, log=_quiet) == MIGRATIONS
        assert catalog(cur) == expected_catalog
        after = data(cur)
    db.rollback()
    assert after.keys() == expected_data.keys()
    for table in ("organizations", "organization_members", "logs"):
        assert after[table] == expected_data[table], table
    assert len(after["runs"]) == len(expected_data["runs"]) == 1
    assert '"checkpoint": {}' in after["runs"][0]


def test_baseline_records_without_running():
    """--baseline: adopt a database that already has the schema, skip the SQL."""
    with fresh_database(migrate=False) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()
        seed(conn)
        with conn.cursor() as cur:
            # Proof the baseline did not run: it would re-create this policy.
            cur.execute("DROP POLICY logs_select ON logs")
        conn.commit()

        baseline = MIGRATIONS[0]
        applied = db_migrate.up(conn, MIGRATIONS, baseline=baseline.version, log=_quiet)
        assert applied == MIGRATIONS
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, baselined FROM automaintainer_migrations.schema_migrations"
                " ORDER BY 1"
            )
            assert cur.fetchall() == [
                (m.version, m.version <= baseline.version) for m in MIGRATIONS
            ]
            cur.execute(
                "SELECT count(*) FROM pg_policies WHERE policyname = 'logs_select'"
            )
            assert cur.fetchone() == (0,)
        conn.rollback()


def test_a_failing_migration_is_rolled_back_and_not_recorded(db, tmp_path):
    bad = tmp_path / "29991231000000_broken.sql"
    bad.write_text(
        "ALTER TABLE runs ADD COLUMN half_applied TEXT;\nSELECT no_such_function();\n",
        encoding="utf-8",
    )
    broken = MIGRATIONS + [db_migrate.Migration("29991231000000", "broken", bad)]
    with pytest.raises(db_migrate.MigrationError, match="29991231000000_broken.sql"):
        db_migrate.up(db, broken, log=_quiet)
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_name = 'runs' AND column_name = 'half_applied'"
        )
        assert cur.fetchone() == (0,)
        cur.execute(
            "SELECT count(*) FROM automaintainer_migrations.schema_migrations"
            " WHERE version = '29991231000000'"
        )
        assert cur.fetchone() == (0,)
    db.rollback()
