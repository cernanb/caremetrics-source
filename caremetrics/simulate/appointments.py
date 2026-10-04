"""Resolve scheduled appointments whose outcome falls inside the run's window.

Each scheduled appointment's fate is decided once, from a random stream keyed by its
own id (see core.entity_rng), so the outcome never depends on how often the
simulator runs:

* no_show:   the front desk marks it 20 minutes to 3 hours after the slot.
* cancelled: cancelled at a moment between booking and the slot.
* completed: the patient is roomed a few minutes early or late, the visit lasts the
             specialty's typical length for the appointment type, and the patient
             checks out. The appointment becomes completed at checkout, and an
             encounter is created with the simulated visit times.

A change is applied by the first run whose window has reached that moment. A visit
still in progress at the run's "now" stays scheduled until a later run.

Rates and visit lengths are the seed's (caremetrics.seed.appointments and the
specialty profiles), so the simulated history continues the seeded one.

Timestamps: the appointment's updated_at is set by the trigger to the run time.
Encounters are inserted (no trigger on INSERT) with updated_at = the window end,
which is the same instant, so every change is visible to cursor-based syncs.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import namedtuple_row

from caremetrics.db import copy_rows
from caremetrics.seed.appointments import (
    CANCEL_RATE,
    DURATION_FACTOR,
    NO_SHOW_RATE,
    NO_SHOW_RATE_DEFAULT,
    TELEHEALTH_NO_SHOW_FACTOR,
    URGENT_CANCEL_RATE,
)
from caremetrics.seed.config import SeedSettings, uuid7
from caremetrics.seed.providers import SPECIALTIES
from caremetrics.simulate.core import SimulationError, Window, entity_rng

SPECIALTY_BY_NAME = {s.name: s for s in SPECIALTIES}

SCHEDULED_APPOINTMENTS = """
    select a.id, a.patient_id, a.provider_id, a.location_id, a.scheduled_at,
           a.appointment_type, a.created_at,
           p.specialty, pp.patient_id is not null as has_profile, py.payer_type
    from appointments a
    join providers p on p.id = a.provider_id
    left join simulator.patient_profiles pp on pp.patient_id = a.patient_id
    left join payers py on py.id = pp.payer_id
    where a.status = 'scheduled'
"""

ENCOUNTER_COLUMNS = ("id", "appointment_id", "patient_id", "provider_id", "location_id",
                     "started_at", "completed_at", "created_at", "updated_at")


@dataclass(frozen=True)
class Outcome:
    status: str
    happens_at: datetime                 # when the change takes effect in simulated time
    visit_start: datetime | None = None  # completed visits only
    visit_end: datetime | None = None


def decide(settings: SeedSettings, appointment) -> Outcome:
    """The appointment's fate. Pure: same appointment in, same outcome out."""
    rng = entity_rng(settings, "appointment-outcome", appointment.id)
    # Draw every value up front, in a fixed order, so the stream never depends on the branch.
    roll = rng.random()
    cancel_fraction = rng.random()
    no_show_marked_after = timedelta(minutes=rng.uniform(20, 180))
    arrival_offset = timedelta(minutes=rng.triangular(-5, 25, 5))
    length_fraction = rng.random()
    checkout_after = timedelta(minutes=rng.uniform(1, 20))

    no_show_rate = NO_SHOW_RATE.get(appointment.payer_type, NO_SHOW_RATE_DEFAULT)
    if appointment.appointment_type == "telehealth":
        no_show_rate *= TELEHEALTH_NO_SHOW_FACTOR
    cancel_rate = URGENT_CANCEL_RATE if appointment.appointment_type == "urgent" else CANCEL_RATE

    if roll < no_show_rate:
        return Outcome("no_show", appointment.scheduled_at + no_show_marked_after)
    if roll < no_show_rate + cancel_rate:
        booked, slot = appointment.created_at, appointment.scheduled_at
        return Outcome("cancelled", booked + (slot - booked) * cancel_fraction)

    low, high = SPECIALTY_BY_NAME[appointment.specialty].visit_minutes
    minutes = (low + (high - low) * length_fraction) * DURATION_FACTOR[appointment.appointment_type]
    visit_start = max(appointment.scheduled_at + arrival_offset, appointment.created_at)
    visit_end = visit_start + timedelta(minutes=minutes)
    return Outcome("completed", visit_end + checkout_after, visit_start, visit_end)


def resolve(conn: psycopg.Connection, settings: SeedSettings, window: Window) -> dict[str, int]:
    """Apply every outcome that has happened by window.end. Returns counts by kind."""
    with conn.cursor(row_factory=namedtuple_row) as cur:
        scheduled = cur.execute(SCHEDULED_APPOINTMENTS).fetchall()

    missing_profiles = sum(1 for a in scheduled if not a.has_profile)
    if missing_profiles:
        raise SimulationError(f"{missing_profiles} scheduled appointments belong to patients without a profile")

    due = [
        (appointment, outcome)
        for appointment in scheduled
        if (outcome := decide(settings, appointment)).happens_at <= window.end
    ]
    if not due:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            """
            update appointments a set status = v.status
            from unnest(%s::uuid[], %s::text[]) as v(id, status)
            where a.id = v.id and a.status = 'scheduled'
            """,
            ([a.id for a, _ in due], [o.status for _, o in due]),
        )
        if cur.rowcount != len(due):
            raise SimulationError(f"expected to update {len(due)} appointments, updated {cur.rowcount}")

    completed = [(a, o) for a, o in due if o.status == "completed"]
    copy_rows(
        conn,
        "encounters",
        ENCOUNTER_COLUMNS,
        (
            (uuid7(o.visit_start, entity_rng(settings, "encounter-id", a.id)), a.id, a.patient_id,
             a.provider_id, a.location_id, o.visit_start, o.visit_end, o.visit_start, window.end)
            for a, o in completed
        ),
    )

    counts = {f"appointments_{status}": 0 for status in ("completed", "no_show", "cancelled")}
    for _, outcome in due:
        counts[f"appointments_{outcome.status}"] += 1
    counts["encounters_created"] = len(completed)
    return counts