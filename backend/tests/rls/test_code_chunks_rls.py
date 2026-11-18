"""code_chunks (pgvector) and match_code_chunks against a real PostgreSQL (#526).

Skipped unless DATABASE_URL_RLS_TEST is set; see test_rls_policies.py for the
URL. The server must have the pgvector extension available (the
``pgvector/pgvector:pg16`` image, or Supabase itself): the migration creates
it in the ``extensions`` schema, as Supabase recommends.

Setup mirrors test_rls_policies.py: a fresh database, the Supabase shim, the
migrations through scripts/db-migrate, then two tenants (org A and org B)
whose repositories hold chunks with *identical* embeddings, so any leak
across tenants would show up as a top match.
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

DIM = 768
USER_A = "aaaaaaaa-0000-4000-8000-000000000001"
USER_B = "bbbbbbbb-0000-4000-8000-000000000002"
ORG_A = "aaaaaaaa-0000-4000-8000-0000000000a0"
ORG_B = "bbbbbbbb-0000-4000-8000-0000000000b0"
# (org, owner, installation id, repository ids)
TENANTS = (
    (ORG_A, USER_A, 1001, (1002, 1003)),
    (ORG_B, USER_B, 2001, (2002,)),
)
REPO_A, REPO_A2, REPO_B = 1002, 1003, 2002


def vec(*hot: int) -> str:
    """A pgvector literal of DIM floats with 1.0 at the ``hot`` positions."""
    values = ["0"] * DIM
    for i in hot:
        values[i] = "1"
    return "[" + ",".join(values) + "]"


def _set_user(cur, user_id):
    cur.execute(
        "SELECT set_config('request.jwt.claim.sub', %s, true)", (user_id or "",)
    )


def _insert_chunk(cur, org, repo, path, content, embedding, commit="c1"):
    cur.execute(
        "INSERT INTO code_chunks (org_id, repository_id, commit_sha, path, symbol,"
        " start_line, end_line, content, content_sha, embedding)"
        " VALUES (%s, %s, %s, %s, 'f', 1, 2, %s, md5(%s), %s::extensions.vector)",
        (org, repo, commit, path, content, content, embedding),
    )


def _seed(conn):
    with conn.cursor() as cur:
        for org, owner, installation, repos in TENANTS:
            cur.execute(
                "INSERT INTO auth.users (id, email) VALUES (%s, %s)",
                (owner, f"{owner[:8]}@example.test"),
            )
            _set_user(cur, owner)  # on_org_created makes the creator the owner
            slug = f"org-{owner[:4]}"
            cur.execute(
                "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
                (org, slug, slug),
            )
            _set_user(cur, None)
            cur.execute(
                "INSERT INTO github_installations (id, org_id, account_login,"
                " account_type, target_type, permissions, events)"
                " VALUES (%s, %s, %s, 'Organization', 'Organization', '{}', '[]')",
                (installation, org, slug),
            )
            for repo in repos:
                cur.execute(
                    "INSERT INTO repositories (id, org_id, github_installation_id,"
                    " full_name, name, owner_login, owner_type)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'Organization')",
                    (repo, org, installation, f"{slug}/r{repo}", f"r{repo}", slug),
                )
        # Same embedding in every repository: only the filters separate them.
        _insert_chunk(cur, ORG_A, REPO_A, "a.py", "def a(): pass", vec(0))
        _insert_chunk(cur, ORG_A, REPO_A, "a2.py", "def a2(): pass", vec(1))
        _insert_chunk(cur, ORG_A, REPO_A2, "other.py", "def o(): pass", vec(0))
        _insert_chunk(cur, ORG_B, REPO_B, "secret.py", "B_SECRET = 1", vec(0))
    conn.commit()


@pytest.fixture(scope="module")
def db():
    if psycopg2 is None:
        pytest.fail("psycopg2 is required for the RLS tests")
    admin = psycopg2.connect(DATABASE_URL)
    admin.autocommit = True
    name = f"automaintainer_rag_{uuid.uuid4().hex[:12]}"
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    conn = None
    try:
        conn = psycopg2.connect(make_dsn(DATABASE_URL, dbname=name))
        with conn.cursor() as cur:
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
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            _set_user(cur, user_id)
            yield cur
    finally:
        conn.rollback()


def attempt(cur, statement, params=()):
    """Run one write; its rowcount, or "denied" on an RLS/privilege error."""
    cur.execute("SAVEPOINT attempt")
    try:
        cur.execute(statement, params)
    except psycopg2.errors.InsufficientPrivilege:
        cur.execute("ROLLBACK TO SAVEPOINT attempt")
        return "denied"
    rowcount = cur.rowcount
    cur.execute("RELEASE SAVEPOINT attempt")
    return rowcount


def match(cur, org, repo, embedding, k=10):
    cur.execute(
        "SELECT path, symbol, start_line, end_line, content, similarity"
        " FROM match_code_chunks(%s, %s, %s::extensions.vector, %s)",
        (org, repo, embedding, k),
    )
    return cur.fetchall()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_vector_extension_lives_in_the_extensions_schema(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT n.nspname FROM pg_extension e"
            " JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
        )
        assert cur.fetchone() == ("extensions",)
    db.rollback()


def test_embedding_column_has_768_dimensions_and_an_hnsw_cosine_index(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a"
            " WHERE a.attrelid = 'public.code_chunks'::regclass"
            " AND a.attname = 'embedding'"
        )
        assert cur.fetchone()[0] in ("vector(768)", "extensions.vector(768)")
        cur.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = 'public' AND tablename = 'code_chunks'"
        )
        defs = [row[0] for row in cur.fetchall()]
    db.rollback()
    assert any("USING hnsw" in d and "vector_cosine_ops" in d for d in defs), defs


def test_rls_is_enabled(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity FROM pg_class WHERE oid = 'public.code_chunks'::regclass"
        )
        assert cur.fetchone() == (True,)
    db.rollback()


def test_match_function_is_security_invoker_with_a_fixed_search_path(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT prosecdef, proconfig FROM pg_proc WHERE proname = 'match_code_chunks'"
        )
        rows = cur.fetchall()
    db.rollback()
    assert len(rows) == 1
    secdef, config = rows[0]
    assert secdef is False
    # An empty search_path (the Supabase linter's recommendation): every
    # object in the body is schema-qualified, so nothing a caller puts on
    # their own search_path can shadow it.
    assert 'search_path=""' in (config or [])


def test_the_same_chunk_cannot_be_stored_twice_for_a_commit(db):
    with session(db, "service_role") as cur:
        cur.execute("SAVEPOINT dup")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_chunk(cur, ORG_A, REPO_A, "a.py", "def a(): pass", vec(0))
        cur.execute("ROLLBACK TO SAVEPOINT dup")
        # Another commit is a different key.
        _insert_chunk(cur, ORG_A, REPO_A, "a.py", "def a(): pass", vec(0), "c2")


def test_chunks_go_with_their_repository(db):
    with session(db, "service_role") as cur:
        cur.execute("DELETE FROM repositories WHERE id = %s", (REPO_A2,))
        cur.execute(
            "SELECT count(*) FROM code_chunks WHERE repository_id = %s", (REPO_A2,)
        )
        assert cur.fetchone()[0] == 0


# --------------------------------------------------------------------------
# Reads: members see their organization's chunks only
# --------------------------------------------------------------------------


@pytest.mark.parametrize("user, org", [(USER_A, ORG_A), (USER_B, ORG_B)])
def test_members_read_only_their_own_orgs_chunks(db, user, org):
    with session(db, "authenticated", user) as cur:
        cur.execute("SELECT DISTINCT org_id::text FROM code_chunks")
        assert {row[0] for row in cur.fetchall()} == {org}


def test_anon_sees_nothing(db):
    with session(db, "anon") as cur:
        cur.execute("SELECT count(*) FROM code_chunks")
        assert cur.fetchone()[0] == 0


def test_cross_org_match_returns_nothing_for_a_member(db):
    """User A asks for org B's repository: RLS hides it, even through the RPC."""
    with session(db, "authenticated", USER_A) as cur:
        assert match(cur, ORG_B, REPO_B, vec(0)) == []
        # Naming their own org with B's repository does not help either.
        assert match(cur, ORG_A, REPO_B, vec(0)) == []


def test_member_match_returns_own_repository_ranked_by_similarity(db):
    with session(db, "authenticated", USER_A) as cur:
        rows = match(cur, ORG_A, REPO_A, vec(0))
    assert [r[0] for r in rows] == ["a.py", "a2.py"]
    path, symbol, start, end, content, similarity = rows[0]
    assert (symbol, start, end, content) == ("f", 1, 2, "def a(): pass")
    assert similarity == pytest.approx(1.0)
    assert rows[1][5] == pytest.approx(0.0)


def test_service_role_match_is_filtered_by_org_and_repository(db):
    """The backend bypasses RLS; the function's own filters still hold."""
    with session(db, "service_role") as cur:
        assert [r[0] for r in match(cur, ORG_A, REPO_A, vec(0))] == ["a.py", "a2.py"]
        assert [r[0] for r in match(cur, ORG_B, REPO_B, vec(0))] == ["secret.py"]
        # Mismatched org and repository: nothing, not B's secret.
        assert match(cur, ORG_A, REPO_B, vec(0)) == []


def test_match_honours_k(db):
    with session(db, "service_role") as cur:
        assert len(match(cur, ORG_A, REPO_A, vec(0), k=1)) == 1
        assert match(cur, ORG_A, REPO_A, vec(0), k=0) == []


def test_anon_cannot_call_match(db):
    with session(db, "anon") as cur:
        assert (
            attempt(cur, "SELECT match_code_chunks(%s, %s, NULL, 5)", (ORG_A, REPO_A))
            == "denied"
        )


# --------------------------------------------------------------------------
# Writes: service role only
# --------------------------------------------------------------------------


@pytest.mark.parametrize("org, repo", [(ORG_A, REPO_A), (ORG_B, REPO_B)])
def test_members_cannot_insert_chunks_in_any_org(db, org, repo):
    with session(db, "authenticated", USER_A) as cur:
        statement = (
            "INSERT INTO code_chunks (org_id, repository_id, commit_sha, path,"
            " start_line, end_line, content, content_sha, embedding)"
            " VALUES (%s, %s, 'x', 'evil.py', 1, 1, 'x', 'x', %s::extensions.vector)"
        )
        assert attempt(cur, statement, (org, repo, vec(0))) == "denied"


@pytest.mark.parametrize("org", [ORG_A, ORG_B])
def test_members_cannot_update_or_delete_chunks(db, org):
    """No UPDATE or DELETE policy: the statements match no row."""
    with session(db, "authenticated", USER_A) as cur:
        update = "UPDATE code_chunks SET content = 'pwned' WHERE org_id = %s"
        assert attempt(cur, update, (org,)) == 0
        assert attempt(cur, "DELETE FROM code_chunks WHERE org_id = %s", (org,)) == 0
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE content = 'pwned')"
            " FROM code_chunks WHERE org_id = %s",
            (org,),
        )
        total, pwned = cur.fetchone()
        assert total > 0 and pwned == 0


def test_service_role_writes_and_deletes_chunks(db):
    with session(db, "service_role") as cur:
        _insert_chunk(cur, ORG_B, REPO_B, "new.py", "def n(): pass", vec(2), "c9")
        assert (
            attempt(
                cur,
                "DELETE FROM code_chunks WHERE repository_id = %s AND commit_sha <> 'c9'",
                (REPO_B,),
            )
            == 1
        )


# --------------------------------------------------------------------------
# Org transfer: chunks follow their repository (round 18)
# --------------------------------------------------------------------------

REPO_MOVED = 3001
ORG_TRANSFER = next(m for m in MIGRATIONS if m.name == "code_chunks_org_transfer")


def _add_repository(cur, repo, org=ORG_A, installation=1001, slug="org-aaaa"):
    cur.execute(
        "INSERT INTO repositories (id, org_id, github_installation_id,"
        " full_name, name, owner_login, owner_type)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'Organization')",
        (repo, org, installation, f"{slug}/r{repo}", f"r{repo}", slug),
    )


def test_chunks_reference_their_repository_and_its_org_with_cascades(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid), confupdtype, confdeltype"
            " FROM pg_constraint WHERE conname = 'code_chunks_repository_org_fkey'"
        )
        ((definition, on_update, on_delete),) = cur.fetchall()
    db.rollback()
    assert "(repository_id, org_id)" in definition
    assert "repositories(id, org_id)" in definition
    assert (on_update, on_delete) == ("c", "c")  # CASCADE, CASCADE


def test_a_moved_repository_takes_its_chunks_and_the_old_org_loses_them(db):
    """An operator moves a repository row to org B (deliberately, with the
    immutability trigger off): its chunks move in the same statement, org A's
    members cannot read them any more, through the table or the RPC, and the
    service role asking with org A's scope gets nothing either."""
    try:
        with db.cursor() as cur:
            _add_repository(cur, REPO_MOVED)
            _insert_chunk(cur, ORG_A, REPO_MOVED, "moved.py", "def m(): 1", vec(0))
            cur.execute(
                "ALTER TABLE repositories DISABLE TRIGGER repositories_org_immutable"
            )
            cur.execute(
                "UPDATE repositories SET org_id = %s, github_installation_id = 2001"
                " WHERE id = %s",
                (ORG_B, REPO_MOVED),
            )
            cur.execute(
                "ALTER TABLE repositories ENABLE TRIGGER repositories_org_immutable"
            )
            cur.execute(
                "SELECT org_id::text FROM code_chunks WHERE repository_id = %s",
                (REPO_MOVED,),
            )
            assert [r[0] for r in cur.fetchall()] == [ORG_B]

            cur.execute("SET LOCAL ROLE authenticated")
            _set_user(cur, USER_A)
            cur.execute(
                "SELECT count(*) FROM code_chunks WHERE repository_id = %s",
                (REPO_MOVED,),
            )
            assert cur.fetchone()[0] == 0
            assert match(cur, ORG_A, REPO_MOVED, vec(0)) == []
            assert match(cur, ORG_B, REPO_MOVED, vec(0)) == []
            _set_user(cur, USER_B)
            assert [r[0] for r in match(cur, ORG_B, REPO_MOVED, vec(0))] == ["moved.py"]
            cur.execute("RESET ROLE")
            cur.execute("SET LOCAL ROLE service_role")
            assert match(cur, ORG_A, REPO_MOVED, vec(0)) == []
    finally:
        db.rollback()


def test_the_immutability_trigger_still_refuses_a_plain_move(db):
    try:
        with db.cursor() as cur:
            cur.execute("SAVEPOINT move")
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "UPDATE repositories SET org_id = %s WHERE id = %s",
                    (ORG_B, REPO_A),
                )
            cur.execute("ROLLBACK TO SAVEPOINT move")
            cur.execute(
                "SELECT DISTINCT org_id::text FROM code_chunks WHERE repository_id = %s",
                (REPO_A,),
            )
            assert [r[0] for r in cur.fetchall()] == [ORG_A]
    finally:
        db.rollback()


@pytest.mark.parametrize("org, repo", [(ORG_B, REPO_A), (ORG_A, REPO_B)])
def test_a_chunk_filed_under_another_org_than_its_repository_is_refused(db, org, repo):
    """The indexer's read-then-write race: the write fails, it never leaks."""
    with session(db, "service_role") as cur:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            _insert_chunk(cur, org, repo, "stale.py", "def s(): 1", vec(0), "c7")


def test_the_migration_drops_stale_rows_and_is_re_runnable(db):
    """Rows written before the migration under the wrong org are deleted
    when it runs (they are re-indexed later); running it twice is a no-op."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "ALTER TABLE code_chunks DROP CONSTRAINT code_chunks_repository_org_fkey"
            )
            _insert_chunk(cur, ORG_B, REPO_A, "stale.py", "def s(): 1", vec(0), "c8")
            cur.execute(ORG_TRANSFER.sql)
            cur.execute(ORG_TRANSFER.sql)
            cur.execute(
                "SELECT count(*) FROM code_chunks WHERE org_id = %s"
                " AND repository_id = %s",
                (ORG_B, REPO_A),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT count(*) FROM code_chunks WHERE repository_id = %s", (REPO_A,)
            )
            assert cur.fetchone()[0] > 0  # the repository's own rows stay
            cur.execute(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conname = 'code_chunks_repository_org_fkey'"
            )
            assert cur.fetchone()[0] == 1
    finally:
        db.rollback()
