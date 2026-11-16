"""scripts/import-legacy-history.sql against a real PostgreSQL (#482).

Skipped unless DATABASE_URL_RLS_TEST is set; see test_rls_policies.py for the
URL and .github/workflows/ci-db.yml for the CI job that runs it. The procedure
is a psql script, so it runs through ``psql`` (PATH, or the PSQL environment
variable), as an operator would run it.

legacy_schema_0147b09.sql is supabase_schema.sql at 0147b09, byte for byte
(git blob c4ba6be; the c398f6c revert restored the same blob), the only
pre-multi-tenant version in history. Each test builds a database from it,
seeds legacy runs and logs, runs the procedure around scripts/db-migrate and
checks the result through row level security, as PostgREST would.
"""

import importlib.util
import os
import shutil
import subprocess
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
LEGACY_SQL = Path(__file__).with_name("legacy_schema_0147b09.sql")
PROCEDURE = REPO_ROOT / "scripts" / "import-legacy-history.sql"

_spec = importlib.util.spec_from_file_location(
    "db_migrate", REPO_ROOT / "scripts" / "db-migrate" / "db_migrate.py"
)
db_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_migrate)
MIGRATIONS = db_migrate.discover()

OWNER_A = "aaaaaaaa-0000-4000-8000-000000000001"
MEMBER_A = "aaaaaaaa-0000-4000-8000-000000000002"
OWNER_B = "bbbbbbbb-0000-4000-8000-000000000001"
ORG_A = "aaaaaaaa-0000-4000-8000-0000000000a0"
ORG_B = "bbbbbbbb-0000-4000-8000-0000000000b0"
REPO_A, REPO_B = 1002, 2002  # both named acme/widget, one per org

RUN_DONE = "00000000-0000-4000-8000-000000000001"
RUN_LIVE = "00000000-0000-4000-8000-000000000002"
LOG_IDS = [f"00000000-0000-4000-8000-0000000001{n:02d}" for n in range(1, 4)]


def _quiet(*_):
    pass


if psycopg2 is not None:

    class _Connection(psycopg2.extensions.connection):
        """Keeps the DSN with its password (``dsn`` masks it) for psql."""

        full_dsn = ""


def psql(conn, step, **variables):
    """Run the procedure like an operator; return (exit code, output)."""
    exe = os.environ.get("PSQL") or shutil.which("psql")
    if not exe:
        pytest.fail("psql is required for the legacy import tests (set PSQL)")
    args = [exe, "-X", "-q", "-d", conn.full_dsn, "-v", f"step={step}"]
    for name, value in variables.items():
        args += ["-v", f"{name}={value}"]
    args += ["-f", str(PROCEDURE)]
    done = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout + done.stderr


def ok(conn, step, **variables):
    code, output = psql(conn, step, **variables)
    assert code == 0, output
    return output


@contextmanager
def legacy_database():
    """A new database built from the 0147b09 schema, with legacy rows."""
    if psycopg2 is None:
        pytest.fail(
            "psycopg2 is required for the RLS tests (uv sync --frozen --group dev)"
        )
    admin = psycopg2.connect(DATABASE_URL)
    admin.autocommit = True
    name = f"automaintainer_legacy_{uuid.uuid4().hex[:12]}"
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    conn = None
    try:
        dsn = make_dsn(DATABASE_URL, dbname=name)
        conn = psycopg2.connect(dsn, connection_factory=_Connection)
        conn.full_dsn = dsn
        conn.autocommit = True  # the legacy file has its own BEGIN/COMMIT
        with conn.cursor() as cur:
            cur.execute(SHIM_SQL.read_text(encoding="utf-8"))
            cur.execute(LEGACY_SQL.read_text(encoding="utf-8"))
            # The old worker stored the repository as typed, URL or not.
            cur.execute(
                "INSERT INTO runs (id, repo_name, status, created_at, updated_at)"
                " VALUES (%s, 'https://github.com/Acme/Widget/', 'completed',"
                " '2026-06-20T10:00:00Z', '2026-06-20T10:05:00Z')",
                (RUN_DONE,),
            )
            cur.execute(
                "INSERT INTO runs (id, repo_name, target_issue, status, created_at)"
                " VALUES (%s, 'acme/widget', 7, 'running', '2026-06-21T10:00:00Z')",
                (RUN_LIVE,),
            )
            cur.execute(
                "INSERT INTO logs (id, run_id, agent_name, log_type, message, color,"
                " metadata) VALUES"
                " (%s, %s, 'architect', 'message', 'hello', 'text-blue-500', NULL),"
                " (%s, %s, 'System', 'ui_update', 'UI Update', NULL, '{\"pipeline\": 1}'),"
                " (%s, %s, 'maintainer', 'merge', 'merged', NULL, NULL)",
                (LOG_IDS[0], RUN_DONE, LOG_IDS[1], RUN_DONE, LOG_IDS[2], RUN_LIVE),
            )
        conn.autocommit = False
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


def seed_tenants(conn, *, repo_in_a=True):
    """Orgs A (owner + member) and B (owner), each with an acme/widget repo."""
    with conn.cursor() as cur:
        for user in (OWNER_A, MEMBER_A, OWNER_B):
            cur.execute("INSERT INTO auth.users (id) VALUES (%s)", (user,))
        for org, owner, n, repo in (
            (ORG_A, OWNER_A, 1, REPO_A),
            (ORG_B, OWNER_B, 2, REPO_B),
        ):
            # on_org_created makes auth.uid() the owner.
            cur.execute(
                "SELECT set_config('request.jwt.claim.sub', %s, true)", (owner,)
            )
            cur.execute(
                "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
                (org, f"org-{n}", f"org-{n}"),
            )
            cur.execute(
                "INSERT INTO github_installations (id, org_id, account_login,"
                " account_type, target_type, permissions, events)"
                " VALUES (%s, %s, 'acme', 'Organization', 'Organization', '{}', '[]')",
                (n * 1000 + 1, org),
            )
            if org == ORG_A and not repo_in_a:
                continue
            cur.execute(
                "INSERT INTO repositories (id, org_id, github_installation_id, full_name,"
                " name, owner_login, owner_type) VALUES (%s, %s, %s, 'acme/widget',"
                " 'widget', 'acme', 'Organization')",
                (repo, org, n * 1000 + 1),
            )
        cur.execute(
            "INSERT INTO organization_members (org_id, user_id, role, joined_at)"
            " VALUES (%s, %s, 'member', now())",
            (ORG_A, MEMBER_A),
        )
    conn.commit()


def visible(conn, user, query):
    """Rows ``query`` returns to ``user`` through the API role."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE authenticated")
        cur.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (user,))
        cur.execute(query)
        rows = cur.fetchall()
    conn.rollback()
    return rows


def counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM logs)")
        result = cur.fetchone()
    conn.rollback()
    return result


def migrate(conn):
    ok(conn, "prepare")
    assert "already done" in ok(conn, "prepare")  # re-runnable
    assert db_migrate.up(conn, MIGRATIONS, log=_quiet) == MIGRATIONS
    assert "already done" in ok(conn, "prepare")


def test_prepare_moves_legacy_tables_out_of_the_api():
    with legacy_database() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE VIEW public.recent AS SELECT id, repo_name FROM runs;"
                " GRANT SELECT ON public.recent TO anon"
            )
        conn.commit()

        output = ok(conn, "prepare")
        assert "dropping view public.recent" in output
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n.nspname, c.relname, c.relrowsecurity FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE c.relname IN ('runs', 'logs', 'recent') ORDER BY 1, 2"
            )
            assert cur.fetchall() == [
                ("automaintainer_legacy", "logs", True),
                ("automaintainer_legacy", "runs", True),
            ]
            cur.execute(
                "SELECT count(*) FROM pg_policies"
                " WHERE schemaname = 'automaintainer_legacy'"
            )
            assert cur.fetchone() == (0,)
            cur.execute("SELECT count(*) FROM pg_publication_tables")
            assert cur.fetchone() == (0,)
            for role in ("anon", "authenticated"):
                cur.execute(
                    "SELECT has_schema_privilege(%s, 'automaintainer_legacy', 'USAGE'),"
                    " has_table_privilege(%s, 'automaintainer_legacy.runs', 'SELECT'),"
                    " has_table_privilege(%s, 'automaintainer_legacy.logs', 'SELECT')",
                    (role, role, role),
                )
                assert cur.fetchone() == (False, False, False), role
            cur.execute("SELECT count(*) FROM automaintainer_legacy.logs")
            assert cur.fetchone() == (3,)
        conn.rollback()

        # The baseline now creates fresh tables (without prepare it fails:
        # runs has no org_id).
        assert db_migrate.up(conn, MIGRATIONS, log=_quiet) == MIGRATIONS
        assert "already done" in ok(conn, "prepare")
        assert counts(conn) == (0, 0)


def test_import_is_visible_to_the_chosen_org_only_and_runs_once():
    with legacy_database() as conn:
        migrate(conn)
        seed_tenants(conn)

        output = ok(conn, "import", org_id=ORG_A)
        assert "inserted 2 run(s) and 3 log(s)" in output

        runs_query = (
            "SELECT id::text, org_id::text, user_id::text, repository_id,"
            " github_installation_id, repo_name, target_issue_number, mode, status,"
            " error_message IS NOT NULL, queued_at = created_at, completed_at"
            " FROM runs ORDER BY id"
        )
        expected_runs = [
            (RUN_DONE, ORG_A, OWNER_A, REPO_A, 1001, "acme/widget", None)
            + ("autonomous", "completed", False, True, None),
            (RUN_LIVE, ORG_A, OWNER_A, REPO_A, 1001, "acme/widget", 7)
            + ("targeted_issue", "failed", True, True, None),
        ]
        logs_query = (
            "SELECT id::text, run_id::text, org_id::text, log_type, metadata"
            " FROM logs ORDER BY id"
        )
        expected_logs = [
            (LOG_IDS[0], RUN_DONE, ORG_A, "message", None),
            (LOG_IDS[1], RUN_DONE, ORG_A, "ui_update", {"pipeline": 1}),
            (LOG_IDS[2], RUN_LIVE, ORG_A, "message", {"legacy_log_type": "merge"}),
        ]
        for user in (OWNER_A, MEMBER_A):
            assert visible(conn, user, runs_query) == expected_runs, user
            assert visible(conn, user, logs_query) == expected_logs, user
        assert visible(conn, OWNER_B, "SELECT id FROM runs") == []
        assert visible(conn, OWNER_B, "SELECT id FROM logs") == []

        # A second run inserts nothing and changes nothing.
        before = counts(conn)
        output = ok(conn, "import", org_id=ORG_A)
        assert "inserted 0 run(s) and 0 log(s)" in output
        assert counts(conn) == before == (2, 3)
        assert visible(conn, OWNER_A, runs_query) == expected_runs

        # Re-running with another org must not quietly leave the history in A.
        code, output = psql(conn, "import", org_id=ORG_B)
        assert code != 0 and "another organization" in output
        assert visible(conn, OWNER_B, "SELECT id FROM runs") == []

        ok(conn, "drop")
        with conn.cursor() as cur:
            cur.execute("SELECT to_regnamespace('automaintainer_legacy')")
            assert cur.fetchone() == (None,)
        conn.rollback()
        assert counts(conn) == (2, 3)
        assert "nothing to do" in ok(conn, "drop")


def test_import_is_all_or_nothing_when_a_repository_is_missing():
    with legacy_database() as conn:
        migrate(conn)
        seed_tenants(conn, repo_in_a=False)

        code, output = psql(conn, "import", org_id=ORG_A)
        assert code != 0
        assert "no repository in organization" in output
        assert "acme/widget" in output
        assert counts(conn) == (0, 0)

        # The legacy rows are still there, so drop refuses.
        code, output = psql(conn, "drop")
        assert code != 0 and "have not been imported" in output

        code, output = psql(conn, "import")
        assert code != 0 and "-v org_id" in output
