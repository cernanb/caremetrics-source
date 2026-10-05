"""One simulator run: advance the database through a window of simulated time.

Each run covers the window (end of the previous run, until], where `until` defaults
to now. The first run starts at the seed's anchor (SEED_ANCHOR_DATE), the instant
the seeded data describes.

"Now" is the database's transaction start time, the same value the updated_at
trigger stamps on every row the run changes. Simulated events inside the window get
realistic timestamps of their own (visit times, submission times), while updated_at
records when the row was actually written. A run may stop earlier than now (`until`),
never later: rows it writes still carry the real run time as updated_at, at or after
`until`, so every change stays visible to cursor-based Airbyte syncs.

A run's changes and its simulator.runs row commit in one transaction (the caller's),
so each window of simulated time is processed exactly once, even if a run crashes or
two start at once.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from caremetrics.seed.config import SeedSettings
from caremetrics.simulate import appointments, bookings, claims, encounters, patients
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
    ("patients", patients.simulate),
    ("bookings", bookings.book),
    ("appointments", appointments.resolve),
    ("encounters", encounters.amend),
    ("claims", claims.simulate),
)


@dataclass
class RunResult:
    run_id: int
    window: Window
    profiles_bootstrapped: int
    step_counts: list[tuple[str, dict[str, int]]] = field(default_factory=list)


def _window(conn: psycopg.Connection, anchor: datetime, until: datetime | None) -> Window:
    now = conn.execute("select now()").fetchone()[0]
    if until is not None and until > now:
        raise SimulationError(f"cannot simulate into the future: --until {until:%Y-%m-%d %H:%M:%S%z} is after now")
    last_until = conn.execute("select max(simulated_until) from simulator.runs").fetchone()[0]
    return Window(start=last_until or anchor, end=until or now)


def run(conn: psycopg.Connection, settings: SeedSettings, until: datetime | None = None) -> RunResult | None:
    """Simulate (previous run's end, until or now] inside the caller's transaction.

    Returns None when there is nothing to simulate. Raises SimulationError when running
    would be unsafe; the caller should then roll back.
    """
    if conn.execute("select to_regclass('simulator.runs')").fetchone()[0] is None:
        raise SimulationError("simulator schema not found. Run migrations first: python -m caremetrics.migrate")
    if not conn.execute("select pg_try_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()[0]:
        raise SimulationError("another simulator run is in progress")

    window = _window(conn, settings.now, until)
    if window.end - window.start < MIN_WINDOW:
        return None

    counts: dict[str, int] = {}
    bootstrapped = ensure_profiles(conn, settings)
    if bootstrapped:
        counts["patient_profiles_bootstrapped"] = bootstrapped
    step_counts = []
    for name, step in STEPS:
        result = step(conn, settings, window)
        counts.update(result)
        step_counts.append((name, result))

    run_id = conn.execute(
        """
        insert into simulator.runs (simulated_from, simulated_until, counts)
        values (%s, %s, %s)
        returning id
        """,
        (window.start, window.end, Jsonb(counts)),
    ).fetchone()[0]
    return RunResult(run_id, window, bootstrapped, step_counts)
