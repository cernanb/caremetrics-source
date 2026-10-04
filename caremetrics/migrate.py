"""Plain-SQL migration runner.

Applies migrations/NNN_description.sql in filename order, each in its own
transaction, and records every applied file in schema_migrations together with a
SHA-256 checksum. An already-applied file that is later edited is reported as an
error instead of being silently skipped: schema changes go in a new migration.

    python -m caremetrics.migrate
"""

import hashlib
import re
import sys
from pathlib import Path

from caremetrics.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
FILENAME_PATTERN = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")

# Arbitrary application-chosen key. A session-level advisory lock on it stops two
# runners (e.g. two terminals, or CI racing a developer on Neon) from applying
# migrations at the same time. Postgres releases it when the connection closes.
ADVISORY_LOCK_KEY = 7_240_001

CREATE_SCHEMA_MIGRATIONS = """
create table if not exists schema_migrations (
    version     text        primary key,
    checksum    text        not null,
    applied_at  timestamptz not null default now()
)
"""


class MigrationError(Exception):
    pass


def discover_migrations() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    badly_named = [f.name for f in files if not FILENAME_PATTERN.match(f.name)]
    if badly_named:
        raise MigrationError(
            f"Migration filenames must look like 001_description.sql: {badly_named}"
        )
    return files


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def migrate() -> int:
    """Apply pending migrations and return how many were applied."""
    files = discover_migrations()

    # autocommit=True so each migration gets an explicit transaction of its own
    # via conn.transaction(), rather than everything sharing one implicit one.
    with connect(autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        conn.execute(CREATE_SCHEMA_MIGRATIONS)

        applied = dict(
            conn.execute("select version, checksum from schema_migrations").fetchall()
        )

        missing_files = sorted(set(applied) - {f.stem for f in files})
        if missing_files:
            raise MigrationError(
                f"Applied migrations have no file in {MIGRATIONS_DIR}: {missing_files}"
            )

        applied_count = 0
        for path in files:
            version = path.stem
            sql = path.read_text()

            if version in applied:
                if applied[version] != checksum(sql):
                    raise MigrationError(
                        f"{path.name} was modified after it was applied. "
                        "Add a new migration instead of editing an applied one."
                    )
                print(f"  already applied  {path.name}")
                continue

            # DDL is transactional in Postgres: if any statement in the file fails,
            # the whole file and its schema_migrations row roll back together.
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (version, checksum(sql)),
                )
            print(f"  applied          {path.name}")
            applied_count += 1

        return applied_count


def main() -> None:
    try:
        applied_count = migrate()
    except MigrationError as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if applied_count:
        print(f"Applied {applied_count} migration(s).")
    else:
        print("Database is up to date.")


if __name__ == "__main__":
    main()