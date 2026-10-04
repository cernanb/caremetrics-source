"""Register new patients and edit patient records inside the run's window.

Registrations: about 5.5 new patients a day (the seed's rate in its final months),
more on weekdays, during business hours. Each new patient is generated like a seeded
one (caremetrics.seed.patients): Faker name matching the recorded gender, age from the
same population bands, mostly Colorado residents. Their generator-only profile (home
clinic, coverage, utilization) is stored in simulator.patient_profiles in the same
transaction, so the booking step can schedule visits for them straight away.

Edits: about 3 record changes a day per 10,000 registered patients, each one of
* moved:       a new state of residence (often still Colorado: an in-state move)
* name_change: a new last name
* correction:  a corrected first name
An edit never depends on the record's current values, so applying edits in order
gives the same final record however the runs are split.

A day's counts and times come from that day's random stream and each patient or
edit from its own, so nothing here depends on how often the simulator runs.
"""

from bisect import bisect_right
from datetime import datetime, time, timedelta

import psycopg
from faker import Faker
from psycopg.rows import namedtuple_row

from caremetrics.db import copy_rows
from caremetrics.seed.appointments import CLINIC_TZ
from caremetrics.seed.config import FAKER_LOCALE, SeedSettings, uuid7
from caremetrics.seed.patients import (
    AGE_BANDS,
    GENDERS,
    HOME_CLINIC_WEIGHTS,
    STATES,
    choose_coverage,
    utilization_for_age,
)
from caremetrics.seed.payers import PROFILES, Payer
from caremetrics.simulate.bookings import WEEKDAY_FACTOR
from caremetrics.simulate.core import SimulationError, Window, daily_count, entity_rng, local_days, times_on

REGISTRATIONS_PER_DAY = 5.5
EDITS_PER_PATIENT_DAY = 0.0003
EDIT_KINDS = {"moved": 50, "name_change": 30, "correction": 20}
# Front-desk and online activity, local hour -> relative weight.
OFFICE_HOUR_WEIGHTS = {7: 0.4, 8: 1.0, 9: 1.0, 10: 1.0, 11: 1.0, 12: 0.7, 13: 1.0,
                       14: 1.0, 15: 1.0, 16: 0.9, 17: 0.5, 18: 0.3}

PATIENT_COLUMNS = ("id", "first_name", "last_name", "date_of_birth", "gender", "state",
                   "created_at", "updated_at")
PROFILE_COLUMNS = ("patient_id", "home_location_id", "payer_id", "utilization")
PROFILE_ORDER = {p.name: i for i, p in enumerate(PROFILES)}


def _weighted(rng, weights: dict):
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _first_name(fake: Faker, gender: str) -> str:
    if gender == "female":
        return fake.first_name_female()
    if gender == "male":
        return fake.first_name_male()
    return fake.first_name()


def _day_end(day) -> datetime:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=CLINIC_TZ)


def _register(conn: psycopg.Connection, settings: SeedSettings, window: Window, fake: Faker) -> int:
    with conn.cursor(row_factory=namedtuple_row) as cur:
        # Same orders as the seed's lists, because weighted choices depend on order.
        locations = cur.execute("select id, city, created_at from locations order by created_at").fetchall()
        payers = sorted(
            (Payer(id=r.id, profile=next(p for p in PROFILES if p.name == r.name))
             for r in cur.execute("select id, name from payers")),
            key=lambda p: PROFILE_ORDER[p.name],
        )

    patients, profiles = [], []
    for day in local_days(window, CLINIC_TZ):
        rng = entity_rng(settings, "registrations-day", day.isoformat())
        count = daily_count(rng, REGISTRATIONS_PER_DAY * WEEKDAY_FACTOR[day.weekday()])
        for index, registered_at in enumerate(times_on(rng, day, CLINIC_TZ, OFFICE_HOUR_WEIGHTS, count)):
            if not window.start < registered_at <= window.end:
                continue  # an earlier run registered it, or a later run will
            key = f"{day.isoformat()}:{index}"
            prng = entity_rng(settings, "new-patient", key)
            fake.seed_instance(f"{settings.random_seed}:new-patient:{key}")

            gender = _weighted(prng, GENDERS)
            low, high, _ = prng.choices(AGE_BANDS, weights=[w for *_, w in AGE_BANDS])[0]
            age = prng.randint(low, high)
            date_of_birth = day - timedelta(days=int(age * 365.25) + prng.randrange(365))
            open_clinics = [l for l in locations if l.created_at <= registered_at]
            home = prng.choices(open_clinics, weights=[HOME_CLINIC_WEIGHTS.get(l.city, 10) for l in open_clinics])[0]
            payer = choose_coverage(prng, age, payers)
            patient_id = uuid7(registered_at, prng)

            patients.append((patient_id, _first_name(fake, gender), fake.last_name(), date_of_birth,
                             gender, _weighted(prng, STATES), registered_at, window.end))
            profiles.append((patient_id, home.id, payer.id if payer else None,
                             utilization_for_age(prng, age)))

    copy_rows(conn, "patients", PATIENT_COLUMNS, patients)
    copy_rows(conn, "simulator.patient_profiles", PROFILE_COLUMNS, profiles)
    return len(patients)


def _edit(conn: psycopg.Connection, settings: SeedSettings, window: Window, fake: Faker) -> int:
    with conn.cursor(row_factory=namedtuple_row) as cur:
        patients = cur.execute("select id, gender, created_at from patients order by created_at, id").fetchall()
    registered_at = [p.created_at for p in patients]

    changes: dict = {}  # patient id -> {column: new value}, later edits win
    for day in local_days(window, CLINIC_TZ):
        rng = entity_rng(settings, "edits-day", day.isoformat())
        mean = EDITS_PER_PATIENT_DAY * bisect_right(registered_at, _day_end(day))
        for index, edited_at in enumerate(times_on(rng, day, CLINIC_TZ, OFFICE_HOUR_WEIGHTS, daily_count(rng, mean))):
            if not window.start < edited_at <= window.end:
                continue
            key = f"{day.isoformat()}:{index}"
            erng = entity_rng(settings, "patient-edit", key)
            fake.seed_instance(f"{settings.random_seed}:patient-edit:{key}")
            eligible = bisect_right(registered_at, edited_at)  # only patients registered by then
            if eligible == 0:
                continue
            patient = patients[erng.randrange(eligible)]
            kind = _weighted(erng, EDIT_KINDS)
            if kind == "moved":
                change = {"state": _weighted(erng, STATES)}
            elif kind == "name_change":
                change = {"last_name": fake.last_name()}
            else:
                change = {"first_name": _first_name(fake, patient.gender)}
            changes.setdefault(patient.id, {}).update(change)

    if not changes:
        return 0
    ids = list(changes)
    with conn.cursor() as cur:
        cur.execute(
            """
            update patients p
            set first_name = coalesce(v.first_name, p.first_name),
                last_name  = coalesce(v.last_name, p.last_name),
                state      = coalesce(v.state, p.state)
            from unnest(%s::uuid[], %s::text[], %s::text[], %s::text[]) as v(id, first_name, last_name, state)
            where p.id = v.id
            """,
            (ids, [changes[i].get("first_name") for i in ids], [changes[i].get("last_name") for i in ids],
             [changes[i].get("state") for i in ids]),
        )
        if cur.rowcount != len(ids):
            raise SimulationError(f"expected to edit {len(ids)} patients, edited {cur.rowcount}")
    return len(ids)


def simulate(conn: psycopg.Connection, settings: SeedSettings, window: Window) -> dict[str, int]:
    """Register new patients first, so edits and bookings in the same run can include them."""
    fake = Faker(FAKER_LOCALE)
    counts = {"patients_registered": _register(conn, settings, window, fake),
              "patient_records_edited": _edit(conn, settings, window, fake)}
    return {k: v for k, v in counts.items() if v}
