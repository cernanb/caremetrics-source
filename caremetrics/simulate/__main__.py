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
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from caremetrics.db import connect
from caremetrics.seed.config import load_settings
from caremetrics.simulate import appointments
from caremetrics.simulate.core import SimulationError, Window
from caremetrics.simulate.profiles import ensure_profiles

# Transaction-scoped advisory lock, released automatically at commit or rollback.
# Distinct from the migration runner's key.
ADVISORY_LOCK_KEY = 7_240_002

# Shorter windows are skipped rather than recorded as near-empty runs.
MIN_WINDOW = timedelta(minutes=1)

# Each step applies the changes that happened inside the window and returns counts by
# kind. Order matters: later steps build on what earlier ones changed.
STEPS = (
    ("appointments", appointments.resolve),
)


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
        try:
            bootstrapped = ensure_profiles(conn, settings)
            if bootstrapped:
                counts["patient_profiles_bootstrapped"] = bootstrapped
                print(f"  bootstrapped {bootstrapped:,} patient profiles")
            for name, step in STEPS:
                step_counts = step(conn, settings, window)
                counts.update(step_counts)
                summary = ", ".join(f"{k}={v:,}" for k, v in step_counts.items()) or "no changes"
                print(f"  {name:<13} {summary}")
        except SimulationError as exc:
            # Leaving the `with` block via sys.exit rolls the transaction back.
            sys.exit(f"Simulation aborted: {exc}")

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