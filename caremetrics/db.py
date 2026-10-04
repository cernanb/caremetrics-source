"""Database connection helper.

Every script in this project connects through here, so DATABASE_URL is the only
place connection details live. Switching from local Docker to Neon means
changing that one variable.

Run directly to check connectivity:

    python -m caremetrics.db
"""

import os
from collections.abc import Iterable, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg import sql

APPLICATION_NAME = "caremetrics-source"


def database_url() -> str:
    # Values already present in the real environment win over .env (override=False),
    # so CI or a Neon deployment can inject DATABASE_URL without a .env file.
    load_dotenv(override=False)
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Create a .env file with: cp .env.example .env"
        )
    return url


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    """Open a new connection.

    Default is autocommit=False: work runs in a transaction that commits when a
    `with connect() as conn:` block exits cleanly and rolls back on an exception.
    """
    return psycopg.connect(
        database_url(),
        autocommit=autocommit,
        application_name=APPLICATION_NAME,
    )

def copy_rows(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> int:
    """Bulk-load rows with COPY ... FROM STDIN and return how many were written.

    `rows` may be a generator: rows are streamed to the server as they are produced,
    so large tables are never fully materialised in Python memory. psycopg adapts
    Python values (UUID, datetime, date, Decimal, bool, None) to COPY's text format.

    `table` may be schema-qualified ("simulator.patient_profiles"); each part is
    quoted separately.

    Runs inside the caller's transaction; the caller decides when to commit.
    """
    statement = sql.SQL("copy {table} ({columns}) from stdin").format(
        table=sql.Identifier(*table.split(".")),
        columns=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
    )
    count = 0
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    return count

def main() -> None:
    with connect() as conn:
        database, user, version, timezone = conn.execute(
            """
            select current_database(),
                   current_user,
                   current_setting('server_version'),
                   current_setting('TimeZone')
            """
        ).fetchone()
        print(f"Connected to {conn.info.host}:{conn.info.port}")
        print(f"  database: {database}")
        print(f"  user:     {user}")
        print(f"  server:   PostgreSQL {version}")
        print(f"  timezone: {timezone}")


if __name__ == "__main__":
    main()