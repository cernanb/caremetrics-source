"""Seed the database with the full synthetic dataset.

    python -m caremetrics.seed            # seed an empty database
    python -m caremetrics.seed --reset    # delete all existing data, then seed

Everything runs in one transaction: either the complete dataset is committed or
nothing changes, including the --reset truncation. Same SEED_RANDOM_SEED and
SEED_ANCHOR_DATE produce byte-identical data on every run.
"""

import argparse
import sys
import time
from collections.abc import Callable, Sized
from typing import TypeVar

from caremetrics.db import connect
from caremetrics.seed import appointments, claims, encounters, locations, patients, payers, providers
from caremetrics.seed.config import load_settings

# Dependency order. Names are constants, never user input, so building SQL from them is safe.
TABLES = ("locations", "payers", "providers", "patients", "appointments", "encounters", "claims")

T = TypeVar("T", bound=Sized)


def _step(name: str, load: Callable[[], T]) -> T:
    started = time.perf_counter()
    rows = load()
    print(f"  {name:<13} {len(rows):>7,} rows  {time.perf_counter() - started:5.1f}s")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m caremetrics.seed",
        description="Load the synthetic CareMetrics dataset.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete all existing rows in the seeded tables before seeding",
    )
    args = parser.parse_args()
    settings = load_settings()
    started = time.perf_counter()

    with connect() as conn:
        if conn.execute("select to_regclass('public.claims')").fetchone()[0] is None:
            sys.exit("Schema not found. Run migrations first: python -m caremetrics.migrate")

        has_data = conn.execute(
            "select " + " or ".join(f"exists (select 1 from {t})" for t in TABLES)
        ).fetchone()[0]
        if has_data and not args.reset:
            sys.exit("Database already contains data. Re-run with --reset to replace it.")
        if args.reset:
            # One TRUNCATE covering every table satisfies the foreign keys between them,
            # and like everything else here it rolls back if seeding fails.
            conn.execute("truncate table " + ", ".join(reversed(TABLES)))
            print("Truncated existing data.")

        print(f"Seeding (seed={settings.random_seed}, anchor={settings.anchor_date}):")
        seeded_locations = _step("locations", lambda: locations.seed(conn, settings))
        seeded_payers = _step("payers", lambda: payers.seed(conn, settings))
        seeded_providers = _step("providers", lambda: providers.seed(conn, settings, seeded_locations))
        seeded_patients = _step(
            "patients", lambda: patients.seed(conn, settings, seeded_locations, seeded_payers)
        )
        seeded_appointments = _step(
            "appointments", lambda: appointments.seed(conn, settings, seeded_providers, seeded_patients)
        )
        seeded_encounters = _step(
            "encounters", lambda: encounters.seed(conn, settings, seeded_appointments)
        )
        _step("claims", lambda: claims.seed(conn, settings, seeded_encounters))
    # Leaving the `with` block committed the transaction.

    # Fresh planner statistics after a bulk load (ANALYZE cannot change data, so autocommit is fine).
    with connect(autocommit=True) as conn:
        conn.execute("analyze " + ", ".join(TABLES))

    print(f"Committed in {time.perf_counter() - started:.1f}s.")


if __name__ == "__main__":
    main()