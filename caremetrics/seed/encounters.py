"""Seed encounters from completed appointments.

One encounter per completed appointment; cancelled, no-show and scheduled appointments
never produce one. Patient, provider and location are copied from the appointment
(the composite foreign key would reject anything else).

Timing comes from the appointment generator's visit_start / visit_end, so an
encounter always lies inside its appointment's lifecycle:

    appointment.created_at (booked)
      <= encounter.started_at = created_at (patient roomed, chart opened)
      <  encounter.completed_at (visit ends)
      <= appointment.updated_at (checkout marks the appointment completed)

updated_at is when the chart was last touched: usually the note is signed within
minutes of the visit ending, but some charts are amended days later (late
documentation, addenda), which is also what an incremental sync would pick up.

Preview (seeds dependencies + encounters inside a transaction, prints, rolls back):

    python -m caremetrics.seed.encounters
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from caremetrics.db import connect, copy_rows
from caremetrics.seed import appointments as appointments_seed
from caremetrics.seed import locations as locations_seed
from caremetrics.seed import patients as patients_seed
from caremetrics.seed import payers as payers_seed
from caremetrics.seed import providers as providers_seed
from caremetrics.seed.appointments import Appointment
from caremetrics.seed.config import SeedSettings, load_settings, uuid7

SIGN_OFF_MINUTES = (0, 45)  # note signed this long after the visit ends
AMENDED_SHARE = 0.08
AMENDED_DAYS = (1, 14)

COLUMNS = ("id", "appointment_id", "patient_id", "provider_id", "location_id",
           "started_at", "completed_at", "created_at", "updated_at")


@dataclass(frozen=True, slots=True)
class Encounter:
    id: uuid.UUID
    appointment: Appointment
    started_at: datetime
    completed_at: datetime
    updated_at: datetime

    @property
    def created_at(self) -> datetime:
        return self.started_at


def generate(settings: SeedSettings, appointments: list[Appointment]) -> list[Encounter]:
    rng = settings.rng("encounters")
    encounters: list[Encounter] = []
    for appointment in sorted(
        (a for a in appointments if a.status == "completed"), key=lambda a: a.visit_start
    ):
        started_at, completed_at = appointment.visit_start, appointment.visit_end
        signed_at = completed_at + timedelta(minutes=rng.uniform(*SIGN_OFF_MINUTES))
        if rng.random() < AMENDED_SHARE:
            signed_at = completed_at + timedelta(days=rng.uniform(*AMENDED_DAYS))
        encounters.append(
            Encounter(
                id=uuid7(started_at, rng),
                appointment=appointment,
                started_at=started_at,
                completed_at=completed_at,
                updated_at=min(signed_at, settings.now),
            )
        )
    return encounters


def seed(conn: psycopg.Connection, settings: SeedSettings, appointments: list[Appointment]) -> list[Encounter]:
    """Insert encounters and return them for the claims seeder."""
    encounters = generate(settings, appointments)
    copy_rows(
        conn,
        "encounters",
        COLUMNS,
        (
            (e.id, e.appointment.id, e.appointment.patient.id, e.appointment.provider.id,
             e.appointment.location.id, e.started_at, e.completed_at, e.created_at, e.updated_at)
            for e in encounters
        ),
    )
    return encounters


def main() -> None:
    settings = load_settings()
    with connect() as conn:
        locations = locations_seed.seed(conn, settings)
        payers = payers_seed.seed(conn, settings)
        providers = providers_seed.seed(conn, settings, locations)
        patients = patients_seed.seed(conn, settings, locations, payers)
        appointments = appointments_seed.seed(conn, settings, providers, patients)
        encounters = seed(conn, settings, appointments)
        print(f"Inserted {len(encounters)} encounters (preview, will roll back)\n")

        print("Visit length by specialty (minutes):")
        for specialty, count, p10, median, p90 in conn.execute(
            """
            select p.specialty, count(*),
                   percentile_disc(0.1) within group (order by m),
                   percentile_disc(0.5) within group (order by m),
                   percentile_disc(0.9) within group (order by m)
            from (select provider_id,
                         round(extract(epoch from completed_at - started_at) / 60)::int as m
                  from encounters) e
            join providers p on p.id = e.provider_id
            group by p.specialty order by count(*) desc
            """
        ):
            print(f"  {specialty:<27} {count:>6}   p10 {p10:>3}  median {median:>3}  p90 {p90:>3}")

        amended = conn.execute(
            "select count(*) from encounters where updated_at > completed_at + interval '1 day'"
        ).fetchone()[0]
        print(f"\nCharts amended a day or more after the visit: {amended}")

        print("\nChecks (all should be 0):")
        checks = {
            "completed appointments without an encounter": """
                select count(*) from appointments a
                where a.status = 'completed'
                  and not exists (select 1 from encounters e where e.appointment_id = a.id)""",
            "encounters for non-completed appointments": """
                select count(*) from encounters e join appointments a on a.id = e.appointment_id
                where a.status <> 'completed'""",
            "started before the appointment was booked": """
                select count(*) from encounters e join appointments a on a.id = e.appointment_id
                where e.started_at < a.created_at""",
            "started far from the scheduled time (>1h)": """
                select count(*) from encounters e join appointments a on a.id = e.appointment_id
                where abs(extract(epoch from e.started_at - a.scheduled_at)) > 3600""",
            "completed after the appointment was checked out": """
                select count(*) from encounters e join appointments a on a.id = e.appointment_id
                where e.completed_at > a.updated_at""",
            "completed or updated after now": """
                select count(*) from encounters where completed_at > %(now)s or updated_at > %(now)s""",
        }
        for label, query in checks.items():
            count = conn.execute(query, {"now": settings.now}).fetchone()[0]
            print(f"  {count:>4}  {label}")

        conn.rollback()


if __name__ == "__main__":
    main()
