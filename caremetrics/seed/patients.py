"""Seed patients.

Every patient is synthetic: Faker names, generated dates of birth, no real PHI.

Distributions (approximate, US outpatient flavoured):
* Age at the anchor date follows broad US population bands, 0-100.
* Gender: mostly female/male, with small 'other' and 'unknown' shares.
  First names follow the recorded gender where it is female or male.
* State: ~92% Colorado, the rest from neighbouring states.
* Registration (created_at) runs from the first clinic's go-live to the anchor date
  at a steadily rising rate (practice growth: the last year sees roughly twice the new
  registrations of the first). Most patients are therefore established before the
  history window. Registration is never before the patient's birth.
* ~15% of records were edited after registration (address, name or demographic
  correction), so updated_at > created_at for them.

Generator-only attributes (NOT database columns), used by later seeders:
* home_location: the clinic the patient usually attends. Chosen among clinics live
  when the patient registered, or at the start of the history window for patients
  registered before it (established patients may have moved to a newer clinic).
  All appointments fall inside the window, so the home clinic is always open for them.
* payer: primary coverage, or None for uninsured/self-pay patients, whose encounters
  produce no claim. Coverage follows age: 65+ is mostly Medicare; children have a
  higher Medicaid share. Coverage is fixed per patient (no plan changes in this phase).
* utilization: relative visit frequency. Rises with age, with per-patient variation,
  so a minority of patients account for a large share of appointments.

Preview (inserts locations, payers and patients inside a transaction, prints, rolls back):

    python -m caremetrics.seed.patients
"""

import random
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import psycopg

from caremetrics.db import connect, copy_rows
from caremetrics.seed import locations as locations_seed
from caremetrics.seed import payers as payers_seed
from caremetrics.seed.config import PATIENT_COUNT, SeedSettings, load_settings, uuid7
from caremetrics.seed.locations import Location
from caremetrics.seed.payers import Payer

# (min_age, max_age inclusive, weight)
AGE_BANDS: list[tuple[int, int, float]] = [
    (0, 17, 21),
    (18, 34, 21),
    (35, 49, 19),
    (50, 64, 20),
    (65, 79, 14),
    (80, 100, 5),
]

GENDERS: dict[str, float] = {"female": 52, "male": 46, "other": 1, "unknown": 1}

STATES: dict[str, float] = {"CO": 92, "WY": 3, "NM": 2, "KS": 1, "NE": 1, "UT": 1}

# Relative patient preference for each clinic, keyed by city.
HOME_CLINIC_WEIGHTS: dict[str, float] = {
    "Denver": 30,
    "Aurora": 25,
    "Lakewood": 20,
    "Boulder": 15,
    # Only offered to patients registering after it opens. It serves its own city,
    # which the Denver-area clinics do not, so it draws a large share of new patients.
    "Colorado Springs": 35,
}

# payer_type -> weight; None means uninsured / self-pay.
COVERAGE_CHILD: dict[str | None, float] = {"commercial": 52, "medicaid": 38, "other_government": 4, None: 6}
COVERAGE_ADULT: dict[str | None, float] = {"commercial": 64, "medicaid": 20, "other_government": 5, None: 11}
COVERAGE_SENIOR: dict[str | None, float] = {"medicare": 90, "commercial": 6, "other_government": 4}

# Registration density rises linearly from 1x at the first go-live to
# (1 + GROWTH)x at the anchor date.
GROWTH = 1.0
EDITED_SHARE = 0.15

COLUMNS = ("id", "first_name", "last_name", "date_of_birth", "gender", "state", "created_at", "updated_at")


@dataclass(frozen=True)
class Patient:
    id: uuid.UUID
    first_name: str
    last_name: str
    date_of_birth: date
    gender: str
    state: str
    created_at: datetime
    updated_at: datetime
    # --- generator-only ---
    home_location: Location
    payer: Payer | None
    utilization: float

    def age_on(self, day: date) -> int:
        years = day.year - self.date_of_birth.year
        if (day.month, day.day) < (self.date_of_birth.month, self.date_of_birth.day):
            years -= 1
        return years


def _weighted(rng: random.Random, weights: dict):
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _instant_between(rng: random.Random, start: datetime, end: datetime) -> datetime:
    return start + (end - start) * rng.random()


def _date_of_birth(rng: random.Random, settings: SeedSettings) -> date:
    low, high, _ = rng.choices(AGE_BANDS, weights=[w for *_, w in AGE_BANDS])[0]
    age = rng.randint(low, high)
    # Somewhere within the year of life that makes them `age` on the anchor date.
    return settings.anchor_date - timedelta(days=int(age * 365.25) + rng.randrange(365))


def _registered_at(
    rng: random.Random, settings: SeedSettings, dob: date, first_go_live: datetime
) -> datetime:
    """Sample from density f(x) proportional to 1 + GROWTH * x over [first_go_live, now],
    truncated to start no earlier than the patient's birth.

    x is the fraction of the way from first_go_live to now. The CDF is
    F(x) = (x + GROWTH * x^2 / 2) / (1 + GROWTH / 2); drawing u uniformly from
    [F(birth), 1] and inverting gives a date on the same growth curve, after birth,
    in a single draw (no rejection loop).
    """
    span = settings.now - first_go_live
    born = datetime.combine(dob, time.min, tzinfo=timezone.utc)
    lower = min(max((born - first_go_live) / span, 0.0), 1.0)

    def cdf(x: float) -> float:
        return (x + GROWTH * x * x / 2) / (1 + GROWTH / 2)

    u = cdf(lower) + (1 - cdf(lower)) * rng.random()
    x = ((1 + GROWTH * (2 + GROWTH) * u) ** 0.5 - 1) / GROWTH
    return max(first_go_live + span * x, born)


def _coverage(rng: random.Random, age: int, payers: list[Payer]) -> Payer | None:
    mix = COVERAGE_SENIOR if age >= 65 else COVERAGE_CHILD if age < 18 else COVERAGE_ADULT
    payer_type = _weighted(rng, mix)
    if payer_type is None:
        return None
    candidates = [p for p in payers if p.payer_type == payer_type]
    return rng.choices(candidates, weights=[p.profile.market_share for p in candidates])[0]


def _utilization(rng: random.Random, age: int) -> float:
    # Young children (well-child visits) and older adults visit more often;
    # lognormal noise gives a long tail of high utilizers.
    if age < 3:
        base = 1.6
    elif age < 18:
        base = 0.8
    elif age < 45:
        base = 0.7
    elif age < 65:
        base = 1.0
    elif age < 80:
        base = 1.5
    else:
        base = 1.8
    return base * rng.lognormvariate(0.0, 0.6)


def generate(settings: SeedSettings, locations: list[Location], payers: list[Payer]) -> list[Patient]:
    rng = settings.rng("patients")
    fake = settings.faker("patients")
    first_go_live = min(l.created_at for l in locations)

    patients: list[Patient] = []
    for _ in range(PATIENT_COUNT):
        gender = _weighted(rng, GENDERS)
        if gender == "female":
            first_name = fake.first_name_female()
        elif gender == "male":
            first_name = fake.first_name_male()
        else:
            first_name = fake.first_name()
        last_name = fake.last_name()

        dob = _date_of_birth(rng, settings)
        created_at = _registered_at(rng, settings, dob, first_go_live)
        edited = rng.random() < EDITED_SHARE
        updated_at = _instant_between(rng, created_at, settings.now) if edited else created_at

        home_chosen_at = max(created_at, settings.history_start)
        open_clinics = [l for l in locations if l.created_at <= home_chosen_at]
        home_location = rng.choices(
            open_clinics, weights=[HOME_CLINIC_WEIGHTS.get(l.city, 10) for l in open_clinics]
        )[0]

        age = settings.anchor_date.year - dob.year  # coarse is fine for coverage/utilization
        patients.append(
            Patient(
                id=uuid7(created_at, rng),
                first_name=first_name,
                last_name=last_name,
                date_of_birth=dob,
                gender=gender,
                state=_weighted(rng, STATES),
                created_at=created_at,
                updated_at=updated_at,
                home_location=home_location,
                payer=_coverage(rng, age, payers),
                utilization=_utilization(rng, age),
            )
        )
    return patients


def seed(
    conn: psycopg.Connection, settings: SeedSettings, locations: list[Location], payers: list[Payer]
) -> list[Patient]:
    """Insert patients and return them (with generator-only attributes) for dependent seeders."""
    patients = generate(settings, locations, payers)
    copy_rows(
        conn,
        "patients",
        COLUMNS,
        (
            (p.id, p.first_name, p.last_name, p.date_of_birth, p.gender, p.state, p.created_at, p.updated_at)
            for p in patients
        ),
    )
    return patients


def main() -> None:
    settings = load_settings()
    with connect() as conn:
        locations = locations_seed.seed(conn, settings)
        payers = payers_seed.seed(conn, settings)
        patients = seed(conn, settings, locations, payers)
        print(f"Inserted {len(patients)} patients (preview, will roll back)\n")

        print("Age at anchor date:")
        for band, count in conn.execute(
            """
            select case
                       when age < 18 then '0-17'
                       when age < 35 then '18-34'
                       when age < 50 then '35-49'
                       when age < 65 then '50-64'
                       when age < 80 then '65-79'
                       else '80+'
                   end as band,
                   count(*)
            from (select extract(year from age(%s::date, date_of_birth))::int as age from patients) a
            group by band order by min(age)
            """,
            (settings.anchor_date,),
        ):
            print(f"  {band:<6} {count:>6}")

        print("\nGender:")
        for gender, count in conn.execute(
            "select gender, count(*) from patients group by gender order by count(*) desc"
        ):
            print(f"  {gender:<8} {count:>6}")

        print("\nState:")
        for state, count in conn.execute(
            "select state, count(*) from patients group by state order by count(*) desc"
        ):
            print(f"  {state}  {count:>6}")

        print("\nRegistered per year:")
        for year, count, edited in conn.execute(
            """
            select extract(year from created_at)::int, count(*),
                   count(*) filter (where updated_at > created_at)
            from patients group by 1 order by 1
            """
        ):
            print(f"  {year}  {count:>6}  ({edited} edited since)")

        # Generator-only attributes are not in the database, so summarise from Python.
        print("\nCoverage (generator-only):")
        coverage = Counter(p.payer.payer_type if p.payer else "uninsured" for p in patients)
        for payer_type, count in coverage.most_common():
            print(f"  {payer_type:<17} {count:>6}")

        print("\nHome clinic (generator-only):")
        homes = Counter(p.home_location.city for p in patients)
        for city, count in homes.most_common():
            print(f"  {city:<17} {count:>6}")

        conn.rollback()


if __name__ == "__main__":
    main()
