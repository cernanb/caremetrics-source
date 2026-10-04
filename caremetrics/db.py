"""Database connection helper.

Every script in this project connects through here, so DATABASE_URL is the only
place connection details live. Switching from local Docker to Neon means
changing that one variable.

Run directly to check connectivity:

    python -m caremetrics.db
"""

import os

import psycopg
from dotenv import load_dotenv

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