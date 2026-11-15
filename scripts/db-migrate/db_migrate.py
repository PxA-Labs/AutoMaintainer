"""Apply supabase/migrations to a PostgreSQL database without the Supabase CLI (#473).

    python scripts/db-migrate up                 # apply pending migrations
    python scripts/db-migrate up --baseline      # record the baseline as applied, run the rest
    python scripts/db-migrate status             # list applied and pending migrations
    python scripts/db-migrate schema [--check]   # (re)generate / verify supabase_schema.sql

The database URL comes from DATABASE_URL (or --database-url, which is visible
in the process list). ``up`` and ``status`` need psycopg2 (a backend
dependency: run through ``uv run --project backend`` or the backend venv);
``schema`` needs only the standard library.

Migrations are ``supabase/migrations/<14-digit UTC timestamp>_<name>.sql``,
the layout the Supabase CLI uses, so ``supabase db push`` applies the same
files. Each pending file runs in its own transaction, together with the row
that records it in ``automaintainer_migrations.schema_migrations`` (not in
``public``, so PostgREST does not expose it). A failing migration is rolled
back completely and stops the run. Concurrent runners serialise on an
advisory lock, and a migration recorded by another runner meanwhile is
skipped.

A file's checksum is recorded with it. ``up`` refuses to run when an applied
migration's file has changed since: released migrations are immutable, add a
new one instead. Files must not contain their own BEGIN/COMMIT.

Every migration in this repository is also re-runnable (guarded with IF NOT
EXISTS / OR REPLACE / DROP ... IF EXISTS), which the RLS suite checks. That is
what makes the generated supabase_schema.sql safe to paste into the Supabase
SQL Editor again, and lets ``up`` adopt a database built from the old
single-file schema without --baseline.
"""

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
SCHEMA_FILE = REPO_ROOT / "supabase_schema.sql"

FILENAME_RE = re.compile(r"^(?P<version>\d{14})_(?P<name>[a-z0-9_]+)\.sql$")

TRACKING_SCHEMA = "automaintainer_migrations"
# Arbitrary constant shared by every runner: pg_advisory_xact_lock key.
LOCK_KEY = 473_000_473

TRACKING_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {TRACKING_SCHEMA};
CREATE TABLE IF NOT EXISTS {TRACKING_SCHEMA}.schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    baselined BOOLEAN NOT NULL DEFAULT false,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

SCHEMA_HEADER = """\
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
"""


class MigrationError(Exception):
    """A migration set or database state the runner refuses to act on."""


def _normalise(text: str) -> str:
    # Checksums and the generated file must not depend on the checkout's
    # line endings (core.autocrlf on Windows).
    return text.replace("\r\n", "\n")


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def sql(self) -> str:
        # utf-8-sig: tolerate a BOM added by a Windows editor.
        return _normalise(self.path.read_text(encoding="utf-8-sig"))

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """All migrations in ``directory``, oldest first."""
    migrations: dict[str, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"{path.name}: migration files must be named "
                "<YYYYMMDDHHMMSS>_<lowercase_name>.sql"
            )
        version = match["version"]
        if version in migrations:
            raise MigrationError(
                f"{path.name}: version {version} is also used by "
                f"{migrations[version].filename}"
            )
        migrations[version] = Migration(version, match["name"], path)
    if not migrations:
        raise MigrationError(f"no migrations found in {directory}")
    return [migrations[v] for v in sorted(migrations)]


# --------------------------------------------------------------------------
# supabase_schema.sql
# --------------------------------------------------------------------------


def render_schema(migrations: list[Migration]) -> str:
    parts = [SCHEMA_HEADER]
    for migration in migrations:
        parts.append(
            "\n-- "
            + "=" * 76
            + f"\n-- supabase/migrations/{migration.filename}\n-- "
            + "=" * 76
            + "\n\n"
            + migration.sql.rstrip("\n")
            + "\n"
        )
    return "".join(parts)


def schema_is_current(migrations: list[Migration], path: Path = SCHEMA_FILE) -> bool:
    if not path.exists():
        return False
    return _normalise(path.read_text(encoding="utf-8")) == render_schema(migrations)


def write_schema(migrations: list[Migration], path: Path = SCHEMA_FILE) -> None:
    path.write_text(render_schema(migrations), encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


def applied_migrations(conn) -> dict[str, str]:
    """{version: checksum} recorded in the tracking table (created if missing)."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(TRACKING_DDL)
            cur.execute(
                f"SELECT version, checksum FROM {TRACKING_SCHEMA}.schema_migrations"
            )
            return dict(cur.fetchall())


def changed_migrations(migrations: list[Migration], applied: dict[str, str]):
    return [
        m
        for m in migrations
        if m.version in applied and applied[m.version] != m.checksum
    ]


def up(conn, migrations: list[Migration], *, baseline: str | None = None, log=print):
    """Apply every migration not yet recorded; return the ones applied.

    Migrations with a version <= ``baseline`` are recorded without being run
    (for a database that already has that schema).
    """
    applied = applied_migrations(conn)
    changed = changed_migrations(migrations, applied)
    if changed:
        names = ", ".join(m.filename for m in changed)
        raise MigrationError(
            f"applied migrations were modified since: {names}. Released "
            "migrations are immutable; revert the edit and add a new migration."
        )
    if baseline is not None and baseline not in {m.version for m in migrations}:
        raise MigrationError(f"--baseline {baseline}: no such migration")

    done = []
    for migration in migrations:
        if migration.version in applied:
            continue
        skip = baseline is not None and migration.version <= baseline
        # One transaction per file: the migration and its tracking row commit
        # together or not at all (``with conn`` rolls back on any exception).
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                    cur.execute(
                        f"SELECT 1 FROM {TRACKING_SCHEMA}.schema_migrations"
                        " WHERE version = %s",
                        (migration.version,),
                    )
                    if cur.fetchone():
                        continue  # another runner applied it meanwhile
                    if not skip:
                        cur.execute(migration.sql)
                    cur.execute(
                        f"INSERT INTO {TRACKING_SCHEMA}.schema_migrations"
                        " (version, name, checksum, baselined)"
                        " VALUES (%s, %s, %s, %s)",
                        (migration.version, migration.name, migration.checksum, skip),
                    )
        except Exception as exc:
            raise MigrationError(
                f"{migration.filename} failed and was rolled back: {exc}"
            ) from exc
        log(f"{'baselined' if skip else 'applied'}  {migration.filename}")
        done.append(migration)
    if not done:
        log("database is up to date")
    return done


def status(conn, migrations: list[Migration], log=print) -> list[Migration]:
    """Print each migration's state; return the pending ones."""
    applied = applied_migrations(conn)
    changed = {m.version for m in changed_migrations(migrations, applied)}
    pending = []
    for migration in migrations:
        if migration.version in changed:
            state = "MODIFIED"
        elif migration.version in applied:
            state = "applied"
        else:
            state = "pending"
            pending.append(migration)
        log(f"{state:9} {migration.filename}")
    for version in sorted(set(applied) - {m.version for m in migrations}):
        log(f"{'unknown':9} {version} (recorded in the database, no file here)")
    return pending


def _connect(url: str):
    try:
        import psycopg2
    except ImportError:
        raise MigrationError(
            "psycopg2 is required: run through `uv run --project backend` "
            "or the backend virtualenv"
        ) from None
    return psycopg2.connect(url)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="db-migrate", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--migrations-dir", type=Path, default=MIGRATIONS_DIR, help=argparse.SUPPRESS
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("up", "apply pending migrations"),
        ("status", "list applied and pending migrations"),
    ):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument(
            "--database-url",
            default=os.environ.get("DATABASE_URL"),
            help="PostgreSQL URL (default: $DATABASE_URL)",
        )
        if name == "up":
            cmd.add_argument(
                "--baseline",
                nargs="?",
                const="first",
                metavar="VERSION",
                help="record migrations up to VERSION (default: the first, the "
                "baseline) as applied without running them",
            )
    schema = commands.add_parser("schema", help="generate supabase_schema.sql")
    schema.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if supabase_schema.sql is out of date instead of writing it",
    )
    args = parser.parse_args(argv)

    try:
        migrations = discover(args.migrations_dir)
        if args.command == "schema":
            if args.check:
                if not schema_is_current(migrations):
                    print(
                        "supabase_schema.sql is out of date: run "
                        "`python scripts/db-migrate schema`",
                        file=sys.stderr,
                    )
                    return 1
                print("supabase_schema.sql is up to date")
            else:
                write_schema(migrations)
                print(f"wrote {SCHEMA_FILE.name} from {len(migrations)} migration(s)")
            return 0

        if not args.database_url:
            parser.error("set DATABASE_URL or pass --database-url")
        conn = _connect(args.database_url)
        try:
            if args.command == "status":
                status(conn, migrations)
            else:
                baseline = args.baseline
                if baseline == "first":
                    baseline = migrations[0].version
                up(conn, migrations, baseline=baseline)
        finally:
            conn.close()
    except MigrationError as exc:
        print(f"db-migrate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
