"""Durable generator-only patient attributes.

The seed assigns every patient a home clinic, a payer (or none, for self-pay) and a
utilization weight, but keeps them in Python only (see caremetrics/seed/patients.py).
The simulator needs the same values on every run, so the first run regenerates them
with the seed code and stores them in simulator.patient_profiles.

Regeneration is only valid if the database was seeded with the current
SEED_RANDOM_SEED and SEED_ANCHOR_DATE, so the regenerated IDs are checked against
the database before anything is written.
"""

import psycopg

from caremetrics.db import copy_rows
from caremetrics.seed import locations as locations_seed
from caremetrics.seed import patients as patients_seed
from caremetrics.seed import payers as payers_seed
from caremetrics.seed.config import SeedSettings
from caremetrics.simulate.core import SimulationError

COLUMNS = ("patient_id", "home_location_id", "payer_id", "utilization")


def _ids(conn: psycopg.Connection, table: str) -> set:
    # Table names are module constants below, never user input.
    return {row[0] for row in conn.execute(f"select id from {table}")}


def ensure_profiles(conn: psycopg.Connection, settings: SeedSettings) -> int:
    """Bootstrap simulator.patient_profiles if empty. Returns the number of rows inserted."""
    if conn.execute("select exists (select 1 from simulator.patient_profiles)").fetchone()[0]:
        return 0

    locations = locations_seed.generate(settings)
    payers = payers_seed.generate(settings)
    patients = patients_seed.generate(settings, locations, payers)

    for table, generated in (
        ("locations", {l.id for l in locations}),
        ("payers", {p.id for p in payers}),
        ("patients", {p.id for p in patients}),
    ):
        if _ids(conn, table) != generated:
            raise SimulationError(
                f"The {table} in the database do not match a seed with "
                f"SEED_RANDOM_SEED={settings.random_seed} and SEED_ANCHOR_DATE={settings.anchor_date}. "
                "Check .env, or reseed with: python -m caremetrics.seed --reset"
            )

    return copy_rows(
        conn,
        "simulator.patient_profiles",
        COLUMNS,
        (
            (p.id, p.home_location.id, p.payer.id if p.payer else None, p.utilization)
            for p in patients
        ),
    )
