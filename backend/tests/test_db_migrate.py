"""Static checks of supabase/migrations and scripts/db-migrate (#473).

These run without PostgreSQL. The migrations themselves are applied (twice,
and on top of an older schema) by tests/rls/test_migrations.py in the ci-db
workflow; here we check what can be checked from the files: naming, that the
generated supabase_schema.sql is in sync, that every statement is guarded so
a re-run is safe, and the runner's bookkeeping against a fake connection.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "db_migrate", REPO_ROOT / "scripts" / "db-migrate" / "db_migrate.py"
)
db_migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_migrate)

MIGRATIONS = db_migrate.discover()


def _strip_comments(text):
    return re.sub(r"--[^\n]*", "", text)


# --------------------------------------------------------------------------
# The committed migrations
# --------------------------------------------------------------------------


def test_migrations_are_named_for_the_supabase_cli_and_ordered():
    assert MIGRATIONS[0].name == "baseline"
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    for m in MIGRATIONS:
        assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", m.filename)
        assert m.path.parent == REPO_ROOT / "supabase" / "migrations"


def test_supabase_schema_sql_is_generated_from_the_migrations():
    assert db_migrate.schema_is_current(
        MIGRATIONS
    ), "supabase_schema.sql is out of date: run `python scripts/db-migrate schema`"
    text = (REPO_ROOT / "supabase_schema.sql").read_text(encoding="utf-8")
    assert text.startswith(db_migrate.SCHEMA_HEADER)
    assert "GENERATED FILE - DO NOT EDIT" in text


def test_baseline_never_destroys_data():
    sql = _strip_comments(MIGRATIONS[0].sql).upper()
    for destructive in (
        "DROP TABLE",
        "DROP COLUMN",
        "DROP SCHEMA",
        "TRUNCATE",
        "DELETE FROM",
        "DROP PUBLICATION",
    ):
        assert destructive not in sql, destructive


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda m: m.filename)
def test_every_statement_is_re_runnable(migration):
    """A static approximation of what CI checks by applying the files twice."""
    sql = _strip_comments(migration.sql)
    flat = " ".join(sql.split())
    problems = []
    for match in re.finditer(
        r"\bCREATE (TABLE|SCHEMA|EXTENSION) (?!IF NOT EXISTS)", flat
    ):
        problems.append(match.group(0))
    for match in re.finditer(r"\bCREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)", flat):
        problems.append(match.group(0))
    for match in re.finditer(r"\bCREATE FUNCTION\b", flat):
        problems.append(match.group(0) + " (use CREATE OR REPLACE)")
    for kind, pattern in (
        ("POLICY", r'\bCREATE POLICY ("[^"]+"|\w+) ON (\w+)'),
        ("TRIGGER", r"\bCREATE TRIGGER (\w+) .*? ON (\w+)"),
    ):
        for match in re.finditer(pattern, flat):
            name, table = match.group(1), match.group(2)
            drop = f"DROP {kind} IF EXISTS {name} ON {table};"
            if drop not in flat[: match.start()]:
                problems.append(f"{match.group(0)} without a preceding {drop}")
    for match in re.finditer(r"\bCREATE VIEW (\w+)", flat):
        if f"DROP VIEW IF EXISTS {match.group(1)};" not in flat[: match.start()]:
            problems.append(f"{match.group(0)} without DROP VIEW IF EXISTS")
    for match in re.finditer(r"\bALTER TABLE \w+ ADD COLUMN (?!IF NOT EXISTS)", flat):
        problems.append(match.group(0))
    assert not problems, problems


def test_baseline_keeps_every_block_added_to_the_old_schema():
    sql = MIGRATIONS[0].sql
    for needle in (
        "ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ADD COLUMN IF NOT EXISTS repo_name TEXT",
        'DROP POLICY IF EXISTS "github_install_insert_admin" ON github_installations;',
        'DROP POLICY IF EXISTS "github_install_update_admin" ON github_installations;',
        "CREATE OR REPLACE FUNCTION repository_in_org(",
        "CREATE OR REPLACE FUNCTION run_in_org(",
        "CREATE TRIGGER repositories_org_immutable BEFORE UPDATE OF org_id",
        "AND repository_in_org(repository_id, org_id, github_installation_id)",
        "AND (run_id IS NULL OR run_in_org(run_id, org_id))",
        "WITH (security_invoker = true)",
    ):
        assert needle in sql, needle


# --------------------------------------------------------------------------
# discover / render
# --------------------------------------------------------------------------


def test_discover_rejects_badly_named_files(tmp_path):
    (tmp_path / "20260101000000_ok.sql").write_text("SELECT 1;")
    (tmp_path / "add_column.sql").write_text("SELECT 1;")
    with pytest.raises(db_migrate.MigrationError, match="add_column.sql"):
        db_migrate.discover(tmp_path)


def test_discover_rejects_duplicate_versions(tmp_path):
    (tmp_path / "20260101000000_a.sql").write_text("SELECT 1;")
    (tmp_path / "20260101000000_b.sql").write_text("SELECT 2;")
    with pytest.raises(db_migrate.MigrationError, match="also used by"):
        db_migrate.discover(tmp_path)


def test_discover_orders_by_version_and_ignores_other_files(tmp_path):
    (tmp_path / "20260201000000_second.sql").write_text("SELECT 2;")
    (tmp_path / "20260101000000_first.sql").write_text("SELECT 1;")
    (tmp_path / "notes.txt").write_text("not a migration")
    assert [m.name for m in db_migrate.discover(tmp_path)] == ["first", "second"]


def test_checksum_and_schema_ignore_crlf_checkouts(tmp_path):
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    for d, eol in ((lf, b"\n"), (crlf, b"\r\n")):
        d.mkdir()
        (d / "20260101000000_a.sql").write_bytes(
            b"SELECT 1;" + eol + b"SELECT 2;" + eol
        )
    (a,), (b,) = db_migrate.discover(lf), db_migrate.discover(crlf)
    assert a.checksum == b.checksum
    assert db_migrate.render_schema([a]) == db_migrate.render_schema([b])


def test_a_utf8_bom_is_not_sent_to_the_database(tmp_path):
    # PostgreSQL rejects a leading U+FEFF as a syntax error.
    (tmp_path / "20260101000000_a.sql").write_bytes(b"\xef\xbb\xbfSELECT 1;\n")
    (migration,) = db_migrate.discover(tmp_path)
    assert migration.sql == "SELECT 1;\n"


def test_schema_check_fails_when_out_of_date(tmp_path, capsys):
    (tmp_path / "20260101000000_other.sql").write_text("SELECT 1;\n")
    assert (
        db_migrate.main(["--migrations-dir", str(tmp_path), "schema", "--check"]) == 1
    )
    assert "out of date" in capsys.readouterr().err
    assert db_migrate.main(["schema", "--check"]) == 0


# --------------------------------------------------------------------------
# up() against a fake connection
# --------------------------------------------------------------------------


class FakeDB:
    """Just enough of a psycopg2 connection: transactions and a tracking table."""

    def __init__(self, recorded=None, fail_on=None):
        self.recorded = dict(recorded or {})  # version -> checksum (committed)
        self.fail_on = fail_on
        self.executed = []  # committed migration SQL
        self.transactions = 0
        self._pending_sql = []
        self._pending_rows = {}

    def __enter__(self):
        self.transactions += 1
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.executed += self._pending_sql
            self.recorded.update(self._pending_rows)
        self._pending_sql, self._pending_rows = [], {}
        return False

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, params=None):
        db = self.db
        if statement.startswith("SELECT version, checksum"):
            self._result = list(db.recorded.items())
        elif statement.startswith("SELECT 1 FROM"):
            self._result = [(1,)] if params[0] in db.recorded else []
        elif statement.startswith("INSERT INTO"):
            db._pending_rows[params[0]] = params[2]
        elif statement.startswith(("SELECT pg_advisory", "\nCREATE SCHEMA")):
            self._result = []
        else:
            if db.fail_on and db.fail_on in statement:
                raise RuntimeError("syntax error")
            db._pending_sql.append(statement)

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


@pytest.fixture
def three(tmp_path):
    for i, name in enumerate(("baseline", "two", "three"), start=1):
        (tmp_path / f"2026010{i}000000_{name}.sql").write_text(f"SELECT {i};")
    return db_migrate.discover(tmp_path)


def test_up_applies_pending_in_order_one_transaction_each(three):
    db = FakeDB(recorded={three[0].version: three[0].checksum})
    applied = db_migrate.up(db, three, log=lambda *_: None)
    assert applied == three[1:]
    assert db.executed == ["SELECT 2;", "SELECT 3;"]
    assert set(db.recorded) == {m.version for m in three}
    # one for reading the tracking table, then one per pending migration
    assert db.transactions == 1 + 2
    assert db_migrate.up(db, three, log=lambda *_: None) == []


def test_up_with_baseline_records_without_running(three):
    db = FakeDB()
    db_migrate.up(db, three, baseline=three[0].version, log=lambda *_: None)
    assert db.executed == ["SELECT 2;", "SELECT 3;"]
    assert three[0].version in db.recorded


def test_up_stops_at_a_failing_migration_and_does_not_record_it(three):
    db = FakeDB(fail_on="SELECT 2;")
    with pytest.raises(db_migrate.MigrationError, match="two.sql failed"):
        db_migrate.up(db, three, log=lambda *_: None)
    assert db.executed == ["SELECT 1;"]
    assert set(db.recorded) == {three[0].version}


def test_up_refuses_when_an_applied_migration_was_edited(three):
    db = FakeDB(recorded={three[0].version: "0" * 64})
    with pytest.raises(db_migrate.MigrationError, match="immutable"):
        db_migrate.up(db, three, log=lambda *_: None)
    assert db.executed == []


def test_up_rejects_an_unknown_baseline(three):
    with pytest.raises(db_migrate.MigrationError, match="no such migration"):
        db_migrate.up(FakeDB(), three, baseline="19990101000000", log=lambda *_: None)


def test_status_lists_pending_modified_and_unknown(three):
    db = FakeDB(recorded={three[0].version: "0" * 64, "20250101000000": "x"})
    lines = []
    pending = db_migrate.status(db, three, log=lines.append)
    assert pending == three[1:]
    assert lines[0].startswith("MODIFIED")
    assert any(line.startswith("unknown") for line in lines)


def test_up_and_status_need_a_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        db_migrate.main(["up"])
    assert exc.value.code == 2
