"""Seed providers (clinicians).

Each specialty carries a generator-only profile describing who it sees and how:
eligible patient ages, appointment type mix, typical visit length and charge level.
Those fields are NOT database columns. The appointment, encounter and claim seeders
use them so specialty differences emerge from the data (pediatrics sees children,
dermatology does many procedures, cardiology bills more per visit, ...).

Staffing rules:
* Primary care (family, internal, pediatrics) works at every clinic; specialists
  work at the three established "hub" clinics.
* A provider's created_at is their hire date and is never before their clinic's go-live.
  Most were hired before the history window, some during it (growth), and founding
  staff at a clinic that opens inside the window are hired in its first 90 days.
* A small share have left (active = false). Their updated_at is the departure date,
  which is when the record was last changed; no appointments are booked after it.

Preview (inserts locations + providers inside a transaction, prints, rolls back):

    python -m caremetrics.seed.providers
"""

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

import psycopg

from caremetrics.db import connect, copy_rows
from caremetrics.seed import locations as locations_seed
from caremetrics.seed.config import PROVIDER_COUNT, SeedSettings, load_settings, uuid7
from caremetrics.seed.locations import Location


@dataclass(frozen=True)
class SpecialtyProfile:
    name: str
    headcount: int
    spread: str                          # "all" clinics or only "hubs"
    patient_age: tuple[int, int]         # inclusive age range seen
    female_only: bool
    appointment_types: dict[str, float]  # relative weights
    visit_minutes: tuple[int, int]       # typical encounter length
    base_charge: tuple[int, int]         # billed USD for a standard visit, before type multiplier


SPECIALTIES: list[SpecialtyProfile] = [
    SpecialtyProfile("Family Medicine", 12, "all", (0, 100), False,
        {"follow_up": 35, "annual_wellness": 20, "telehealth": 15, "urgent": 12, "new_patient": 10, "consultation": 5, "procedure": 3},
        (15, 30), (120, 220)),
    SpecialtyProfile("Internal Medicine", 8, "all", (18, 100), False,
        {"follow_up": 45, "annual_wellness": 15, "telehealth": 15, "new_patient": 10, "urgent": 8, "consultation": 5, "procedure": 2},
        (20, 40), (130, 240)),
    SpecialtyProfile("Pediatrics", 7, "all", (0, 17), False,
        {"annual_wellness": 35, "follow_up": 20, "urgent": 20, "new_patient": 10, "telehealth": 10, "consultation": 5},
        (15, 30), (110, 200)),
    SpecialtyProfile("Obstetrics and Gynecology", 5, "hubs", (15, 70), True,
        {"follow_up": 40, "annual_wellness": 20, "procedure": 12, "new_patient": 10, "telehealth": 10, "consultation": 8},
        (20, 40), (150, 300)),
    SpecialtyProfile("Cardiology", 4, "hubs", (30, 95), False,
        {"follow_up": 40, "consultation": 25, "new_patient": 15, "procedure": 12, "telehealth": 8},
        (30, 50), (250, 480)),
    SpecialtyProfile("Orthopedics", 4, "hubs", (10, 95), False,
        {"follow_up": 35, "consultation": 25, "procedure": 20, "new_patient": 15, "telehealth": 5},
        (20, 40), (220, 420)),
    SpecialtyProfile("Dermatology", 3, "hubs", (0, 100), False,
        {"procedure": 35, "follow_up": 30, "new_patient": 20, "consultation": 10, "telehealth": 5},
        (15, 30), (140, 320)),
    SpecialtyProfile("Psychiatry", 3, "hubs", (12, 90), False,
        {"follow_up": 50, "telehealth": 35, "new_patient": 15},
        (30, 60), (160, 300)),
    SpecialtyProfile("Endocrinology", 2, "hubs", (10, 95), False,
        {"follow_up": 50, "telehealth": 20, "new_patient": 15, "consultation": 15},
        (20, 40), (180, 320)),
    SpecialtyProfile("Gastroenterology", 2, "hubs", (18, 95), False,
        {"consultation": 30, "follow_up": 30, "procedure": 25, "new_patient": 10, "telehealth": 5},
        (30, 60), (230, 450)),
]

HUB_COUNT = 3              # the three earliest clinics host specialists
FOUNDING_WINDOW_DAYS = 90  # founding staff hired within this many days of go-live
PRE_WINDOW_HIRE_SHARE = 0.75
DEPARTURE_RATE = 0.08
MIN_TENURE_DAYS = 180

COLUMNS = ("id", "first_name", "last_name", "specialty", "location_id", "active", "created_at", "updated_at")


@dataclass(frozen=True)
class Provider:
    id: uuid.UUID
    first_name: str
    last_name: str
    specialty: SpecialtyProfile
    location: Location
    hired_at: datetime
    departed_at: datetime | None

    @property
    def active(self) -> bool:
        return self.departed_at is None

    @property
    def updated_at(self) -> datetime:
        return self.departed_at or self.hired_at


def _workday_instant(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """A weekday instant in [start, end] during Mountain business hours (15:00-23:00 UTC)."""
    span_days = max((end - start).days, 0)
    day = start.date()
    for _ in range(10):
        day = (start + timedelta(days=rng.randint(0, span_days))).date()
        if day.weekday() < 5:
            break
    instant = datetime.combine(day, time(15), tzinfo=timezone.utc) + timedelta(
        minutes=rng.randrange(8 * 60)
    )
    return min(max(instant, start), end)


def _hired_at(rng: random.Random, location: Location, settings: SeedSettings) -> datetime:
    go_live = location.created_at
    if go_live >= settings.history_start:
        # Clinic opened inside the window: founding staff join shortly after go-live.
        return _workday_instant(
            rng, go_live, min(go_live + timedelta(days=FOUNDING_WINDOW_DAYS), settings.now)
        )
    if rng.random() < PRE_WINDOW_HIRE_SHARE:
        return _workday_instant(rng, go_live, settings.history_start)
    return _workday_instant(rng, settings.history_start, settings.now - timedelta(days=60))


def _departed_at(rng: random.Random, hired_at: datetime, settings: SeedSettings) -> datetime | None:
    leaves = rng.random() < DEPARTURE_RATE  # always drawn, so the stream stays aligned
    earliest = hired_at + timedelta(days=MIN_TENURE_DAYS)
    latest = settings.now - timedelta(days=30)
    if not leaves or earliest >= latest:
        return None
    return _workday_instant(rng, earliest, latest)


def generate(settings: SeedSettings, locations: list[Location]) -> list[Provider]:
    headcount = sum(s.headcount for s in SPECIALTIES)
    if headcount != PROVIDER_COUNT:
        raise ValueError(f"Specialty headcounts sum to {headcount}, expected {PROVIDER_COUNT}")

    rng = settings.rng("providers")
    fake = settings.faker("providers")
    by_go_live = sorted(locations, key=lambda l: l.created_at)
    hubs = by_go_live[:HUB_COUNT]

    providers: list[Provider] = []
    for specialty in SPECIALTIES:
        pool = by_go_live if specialty.spread == "all" else hubs
        offset = rng.randrange(len(pool))  # vary which clinic gets the "extra" provider
        for i in range(specialty.headcount):
            location = pool[(offset + i) % len(pool)]
            hired_at = _hired_at(rng, location, settings)
            providers.append(
                Provider(
                    id=uuid7(hired_at, rng),
                    first_name=fake.first_name(),
                    last_name=fake.last_name(),
                    specialty=specialty,
                    location=location,
                    hired_at=hired_at,
                    departed_at=_departed_at(rng, hired_at, settings),
                )
            )
    return providers


def seed(conn: psycopg.Connection, settings: SeedSettings, locations: list[Location]) -> list[Provider]:
    """Insert providers and return them (with specialty profiles) for dependent seeders."""
    providers = generate(settings, locations)
    copy_rows(
        conn,
        "providers",
        COLUMNS,
        (
            (p.id, p.first_name, p.last_name, p.specialty.name, p.location.id,
             p.active, p.hired_at, p.updated_at)
            for p in providers
        ),
    )
    return providers


def main() -> None:
    settings = load_settings()
    with connect() as conn:
        locations = locations_seed.seed(conn, settings)
        providers = seed(conn, settings, locations)
        print(f"Inserted {len(providers)} providers (preview, will roll back)\n")

        print("By specialty:")
        for specialty, total, inactive in conn.execute(
            """
            select specialty, count(*), count(*) filter (where not active)
            from providers group by specialty order by count(*) desc, specialty
            """
        ):
            print(f"  {specialty:<27} {total:>3}  ({inactive} inactive)")

        print("\nBy clinic:")
        for city, total, specialties, first_hire in conn.execute(
            """
            select l.city, count(*), count(distinct p.specialty), min(p.created_at)::date
            from providers p join locations l on l.id = p.location_id
            group by l.city, l.created_at order by l.created_at
            """
        ):
            print(f"  {city:<17} {total:>3} providers, {specialties:>2} specialties, first hire {first_hire}")

        hired_before_go_live = conn.execute(
            """
            select count(*) from providers p join locations l on l.id = p.location_id
            where p.created_at < l.created_at
            """
        ).fetchone()[0]
        print(f"\nProviders hired before their clinic went live: {hired_before_go_live}")
        conn.rollback()


if __name__ == "__main__":
    main()