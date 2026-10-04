"""Advance the operational database through simulated time.

    python -m caremetrics.simulate             # catch up to now and commit
    python -m caremetrics.simulate --dry-run   # show what would change, then roll back

Each run covers the window (end of the previous run, now]. The first run starts at
the seed's anchor (SEED_ANCHOR_DATE), the instant the seeded data describes.

"Now" is the database's transaction start time, the same value the updated_at
trigger stamps on every row the run changes. Simulated events inside the window get
realistic timestamps of their own (visit times, submission times), while updated_at
records when the row was actually written. That keeps every change visible to
cursor-based Airbyte syncs.

A run's changes and its simulator.runs row commit in one transaction, so each window
of simulated time is processed exactly once, even if a run crashes or two start at once.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from caremetrics.db import connect
from caremetrics.seed.config import load_settings

# Transaction-scoped advisory lock, released automatically at commit or rollback.
# Distinct from the migration runner's key.
ADVISORY_LOCK_KEY = 7_240_002

# Shorter windows are skipped rather than recorded as near-empty runs.
MIN_WINDOW = timedelta(minutes=1)


@dataclass(frozen=True)
class Window:
    start: datetime  # exclusive: end of the previous run, or the seed anchor
    end: datetime    # inclusive: this run's "now"

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M:%S%z} -> {self.end:%Y-%m-%d %H:%M:%S%z} ({self.end - self.start})"


def _window(conn: psycopg.Connection, anchor: datetime) -> Window:
    now = conn.execute("select now()").fetchone()[0]
    last_until = conn.execute("select max(simulated_until) from simulator.runs").fetchone()[0]
    return Window(start=last_until or anchor, end=now)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m caremetrics.simulate",
        description="Advance the CareMetrics operational data to the current time.",
    )
    parser.add_argument("--dry-run", action="store_true", help="report changes, then roll back")
    args = parser.parse_args()
    settings = load_settings()

    with connect() as conn:
        if conn.execute("select to_regclass('simulator.runs')").fetchone()[0] is None:
            sys.exit("Simulator schema not found. Run migrations first: python -m caremetrics.migrate")
        if not conn.execute("select pg_try_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()[0]:
            sys.exit("Another simulator run is in progress.")

        window = _window(conn, settings.now)
        if window.end - window.start < MIN_WINDOW:
            print(f"Nothing to simulate: the last run ended at {window.start:%Y-%m-%d %H:%M:%S%z}.")
            return

        print(f"Simulating {window}")
        counts: dict[str, int] = {}
        # Simulation steps are added here, slice by slice. Each receives the connection
        # and the window, applies its changes, and reports a count.
        if not counts:
            print("  (no simulation steps yet)")

        run_id = conn.execute(
            """
            insert into simulator.runs (simulated_from, simulated_until, counts)
            values (%s, %s, %s)
            returning id
            """,
            (window.start, window.end, Jsonb(counts)),
        ).fetchone()[0]

        if args.dry_run:
            conn.rollback()
            print("Dry run: all changes rolled back.")
            return
    # Leaving the `with` block committed the transaction.
    print(f"Committed run {run_id}.")


if __name__ == "__main__":
    main()