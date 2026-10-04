"""Seed appointments.

Generation runs in two phases.

Phase 1 decides *who* visits *when*:
  Each appointment picks a patient with probability proportional to
  utilization x days the patient is a patient inside the window, so high utilizers
  and long-standing patients visit more, and total volume grows as the patient base
  grows. The visit day is then accepted or rejected by clinic calendar rules:
    * closed Sundays and on major US holidays, a light Saturday schedule
    * mild seasonality (winter respiratory season busier, midsummer quieter)
    * future bookings thin out with distance (few appointments booked 8 weeks out)
  Times are 15-minute slots in clinic-local time (America/Denver, DST aware)
  stored as UTC, with a lunch-hour dip.

Phase 2 walks those visits in chronological order and decides *what* each one is:
  * Specialty: mostly the patient's primary care (pediatrics for children, family or
    internal medicine for adults), with a specialist share that rises with age.
    Specialists are limited to eligible patients (age range, OB/GYN female only),
    and a patient who has seen a specialist usually returns to that specialty.
  * Provider: someone of that specialty employed on that day, strongly preferring the
    provider the patient saw last time. Otherwise a provider at the patient's home
    clinic, favouring smaller panels so workloads stay plausible.
  * Type: from the specialty's mix, with rules that keep history coherent:
    a patient's first completed visit with a specialty is 'new_patient' (for primary
    care only if the patient joined during the window), never again afterwards,
    and 'annual_wellness' at most about once a year.
  * Status: future visits are 'scheduled' (some already cancelled); past visits are
    completed, cancelled or no_show. No-shows are more common for Medicaid and
    uninsured patients and less common for telehealth.
  * Timestamps: created_at is the booking time (lead time depends on type, never
    before the patient registered or the provider was hired, never after "now");
    updated_at is when the status last changed (checkout, cancellation, no-show mark).

Completed appointments also carry generator-only visit_start / visit_end, which the
encounter seeder turns into encounters so the two tables agree exactly.

Preview (seeds dependencies + appointments inside a transaction, prints, rolls back):

    python -m caremetrics.seed.appointments
"""

import random
import uuid
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import cache
from itertools import accumulate
from zoneinfo import ZoneInfo

import psycopg

from caremetrics.db import connect, copy_rows
from caremetrics.seed import locations as locations_seed
from caremetrics.seed import patients as patients_seed
from caremetrics.seed import payers as payers_seed
from caremetrics.seed import providers as providers_seed
from caremetrics.seed.config import APPOINTMENT_COUNT, FUTURE_DAYS, SeedSettings, load_settings, uuid7
from caremetrics.seed.locations import Location
from caremetrics.seed.patients import Patient
from caremetrics.seed.providers import SPECIALTIES, Provider, SpecialtyProfile

CLINIC_TZ = ZoneInfo("America/Denver")

PRIMARY_CARE = ("Family Medicine", "Internal Medicine", "Pediatrics")

# --- Phase 1: calendar --------------------------------------------------------

SATURDAY_ACCEPT = 0.10
# Relative volume by month (accept probability = weight / max weight).
SEASONALITY = {1: 1.15, 2: 1.12, 3: 1.05, 4: 1.0, 5: 0.97, 6: 0.92,
               7: 0.85, 8: 0.95, 9: 1.0, 10: 1.05, 11: 1.05, 12: 0.97}
# Share of slots still open on the last bookable future day (linear from 1.0 today).
FUTURE_TAIL_ACCEPT = 0.15
# Visits starting this close to "now" are skipped so no past visit is still in progress.
IN_PROGRESS_BUFFER = timedelta(hours=3)
# Local start hours 08:00-16:45 and their relative weight (lunch dip at noon).
HOUR_WEIGHTS = {8: 0.9, 9: 1.0, 10: 1.0, 11: 1.0, 12: 0.55, 13: 0.9, 14: 1.0, 15: 1.0, 16: 0.8}

# --- Phase 2: what each visit is ----------------------------------------------

SPECIALIST_SHARE_BY_AGE = ((18, 0.12), (50, 0.25), (200, 0.35))  # (age upper bound, share)
CHILD_PEDIATRICS_SHARE = 0.85
ADULT_FAMILY_MEDICINE_SHARE = 0.60
OBGYN_WEIGHT_BOOST = 3.0
PROVIDER_CONTINUITY = 0.90
SPECIALIST_RETURN = 0.75  # chance a specialist visit returns to an already-seen specialty
WELLNESS_MIN_GAP = timedelta(days=330)

FUTURE_CANCEL_RATE = 0.08
CANCEL_RATE = 0.21
URGENT_CANCEL_RATE = 0.05
NO_SHOW_RATE = {"medicaid": 0.14, None: 0.16}  # by payer_type; None = uninsured
NO_SHOW_RATE_DEFAULT = 0.08
TELEHEALTH_NO_SHOW_FACTOR = 0.5

# Booking lead time in days, by appointment type.
LEAD_DAYS = {
    "urgent": (0.02, 1.2),
    "telehealth": (0.5, 14),
    "follow_up": (7, 90),
    "annual_wellness": (14, 120),
    "new_patient": (7, 60),
    "consultation": (7, 60),
    "procedure": (7, 60),
}
# Visit length multiplier, by appointment type, applied to the specialty's typical length.
DURATION_FACTOR = {
    "new_patient": 1.4, "procedure": 1.6, "annual_wellness": 1.3, "consultation": 1.2,
    "follow_up": 1.0, "urgent": 0.9, "telehealth": 0.7,
}

COLUMNS = ("id", "patient_id", "provider_id", "location_id", "scheduled_at", "status",
           "appointment_type", "created_at", "updated_at")


@dataclass(frozen=True, slots=True)
class Appointment:
    id: uuid.UUID
    patient: Patient
    provider: Provider
    scheduled_at: datetime
    status: str
    appointment_type: str
    created_at: datetime
    updated_at: datetime
    # --- generator-only: set only for completed appointments ---
    visit_start: datetime | None
    visit_end: datetime | None

    @property
    def location(self) -> Location:
        return self.provider.location


# ------------------------------------------------------------------------------
# Phase 1
# ------------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th given weekday of a month (n=-1 for the last one). Monday is 0."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


@cache
def clinic_holidays(year: int) -> frozenset[date]:
    return frozenset({
        date(year, 1, 1),                 # New Year's Day
        _nth_weekday(year, 5, 0, -1),     # Memorial Day
        date(year, 7, 4),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),      # Labor Day
        _nth_weekday(year, 11, 3, 4),     # Thanksgiving
        date(year, 12, 25),               # Christmas Day
    })


def _clinic_open(rng: random.Random, day: date) -> bool:
    if day.weekday() == 6 or day in clinic_holidays(day.year):
        return False
    if day.weekday() == 5 and rng.random() >= SATURDAY_ACCEPT:
        return False
    return rng.random() < SEASONALITY[day.month] / max(SEASONALITY.values())


def _slot(rng: random.Random, day: date) -> datetime:
    hour = rng.choices(list(HOUR_WEIGHTS), weights=list(HOUR_WEIGHTS.values()))[0]
    local = datetime.combine(day, time(hour, rng.choice((0, 15, 30, 45))), tzinfo=CLINIC_TZ)
    return local.astimezone(timezone.utc)


def _sample_visits(rng: random.Random, settings: SeedSettings, patients: list[Patient]) -> list[tuple[datetime, int]]:
    """Phase 1: (scheduled_at, patient index) pairs, unsorted."""
    window_starts = [max(p.created_at, settings.history_start) for p in patients]
    weights = [
        p.utilization * (settings.future_end - start).total_seconds()
        for p, start in zip(patients, window_starts)
    ]
    cumulative = list(accumulate(weights))
    total = cumulative[-1]

    visits: list[tuple[datetime, int]] = []
    while len(visits) < APPOINTMENT_COUNT:
        index = bisect_right(cumulative, rng.random() * total)
        start = window_starts[index]
        instant = start + (settings.future_end - start) * rng.random()
        day = instant.astimezone(CLINIC_TZ).date()
        if not _clinic_open(rng, day):
            continue

        scheduled_at = _slot(rng, day)
        if not (start <= scheduled_at <= settings.future_end):
            continue
        if settings.now - IN_PROGRESS_BUFFER < scheduled_at <= settings.now:
            continue
        if scheduled_at > settings.now:
            days_ahead = (scheduled_at - settings.now) / timedelta(days=1)
            if rng.random() >= 1 - (1 - FUTURE_TAIL_ACCEPT) * days_ahead / FUTURE_DAYS:
                continue

        visits.append((scheduled_at, index))
    return visits


# ------------------------------------------------------------------------------
# Phase 2
# ------------------------------------------------------------------------------

def _weighted(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _employed(provider: Provider, at: datetime) -> bool:
    return provider.hired_at <= at and (provider.departed_at is None or at < provider.departed_at)


class _History:
    """Per-patient state carried through the chronological walk."""

    def __init__(self) -> None:
        self.primary_specialty: dict[tuple[uuid.UUID, bool], str] = {}
        self.preferred_provider: dict[tuple[uuid.UUID, str], Provider] = {}
        self.seen_specialties: set[tuple[uuid.UUID, str]] = set()
        self.last_wellness: dict[uuid.UUID, datetime] = {}
        self.specialists_seen: dict[uuid.UUID, list[str]] = {}
        self.panel_size: Counter[uuid.UUID] = Counter()  # patients who prefer each provider


class _Generator:
    def __init__(self, settings: SeedSettings, providers: list[Provider], rng: random.Random) -> None:
        self.settings = settings
        self.rng = rng
        self.history = _History()
        self.specialties = {s.name: s for s in SPECIALTIES}
        self.providers_by_specialty: dict[str, list[Provider]] = {s.name: [] for s in SPECIALTIES}
        for provider in providers:
            self.providers_by_specialty[provider.specialty.name].append(provider)

    # -- specialty and provider ----------------------------------------------

    def _primary_specialty(self, patient: Patient, age: int) -> SpecialtyProfile:
        child = age < 18
        key = (patient.id, child)
        if key not in self.history.primary_specialty:
            if child:
                name = "Pediatrics" if self.rng.random() < CHILD_PEDIATRICS_SHARE else "Family Medicine"
            else:
                name = "Family Medicine" if self.rng.random() < ADULT_FAMILY_MEDICINE_SHARE else "Internal Medicine"
            self.history.primary_specialty[key] = name
        return self.specialties[self.history.primary_specialty[key]]

    def _specialist(self, patient: Patient, age: int) -> SpecialtyProfile | None:
        seen = [
            name for name in self.history.specialists_seen.get(patient.id, [])
            if self._eligible(self.specialties[name], patient, age)
        ]
        if seen and self.rng.random() < SPECIALIST_RETURN:
            return self.specialties[self.rng.choice(seen)]
        weights: dict[str, float] = {}
        for s in SPECIALTIES:
            if s.name in PRIMARY_CARE or not self._eligible(s, patient, age):
                continue
            weights[s.name] = s.headcount * (OBGYN_WEIGHT_BOOST if s.female_only else 1.0)
        return self.specialties[_weighted(self.rng, weights)] if weights else None

    @staticmethod
    def _eligible(specialty: SpecialtyProfile, patient: Patient, age: int) -> bool:
        if not specialty.patient_age[0] <= age <= specialty.patient_age[1]:
            return False
        return not specialty.female_only or patient.gender == "female"

    def _provider(self, patient: Patient, specialty: SpecialtyProfile, at: datetime) -> Provider | None:
        candidates = [p for p in self.providers_by_specialty[specialty.name] if _employed(p, at)]
        if not candidates:
            return None
        key = (patient.id, specialty.name)
        preferred = self.history.preferred_provider.get(key)
        if preferred in candidates and self.rng.random() < PROVIDER_CONTINUITY:
            return preferred
        nearby = [p for p in candidates if p.location.id == patient.home_location.id]
        pool = nearby or candidates
        panel = self.history.panel_size
        chosen = self.rng.choices(pool, weights=[1 / (1 + panel[p.id]) ** 2 for p in pool])[0]
        if preferred not in candidates:
            if preferred is not None:
                panel[preferred.id] -= 1
            panel[chosen.id] += 1
            self.history.preferred_provider[key] = chosen
        return chosen

    def _specialty_and_provider(self, patient: Patient, age: int, at: datetime):
        share = next(s for upper, s in SPECIALIST_SHARE_BY_AGE if age < upper)
        if self.rng.random() < share:
            specialty = self._specialist(patient, age)
            if specialty is not None:
                provider = self._provider(patient, specialty, at)
                if provider is not None:
                    return specialty, provider
        # Primary care, or fallback when no eligible specialist is employed that day.
        specialty = self._primary_specialty(patient, age)
        provider = self._provider(patient, specialty, at)
        if provider is None:
            # e.g. a child whose clinic briefly has no pediatrician: family medicine sees them.
            specialty = self.specialties["Family Medicine"]
            provider = self._provider(patient, specialty, at)
        return specialty, provider

    # -- type, status, timestamps --------------------------------------------

    def _appointment_type(self, patient: Patient, specialty: SpecialtyProfile, at: datetime) -> str:
        seen = (patient.id, specialty.name) in self.history.seen_specialties
        if not seen and (specialty.name not in PRIMARY_CARE or patient.created_at >= self.settings.history_start):
            return "new_patient"
        weights = {t: w for t, w in specialty.appointment_types.items() if t != "new_patient"}
        appointment_type = _weighted(self.rng, weights)
        if appointment_type == "annual_wellness":
            last = self.history.last_wellness.get(patient.id)
            if last is not None and at - last < WELLNESS_MIN_GAP:
                appointment_type = "follow_up"
        return appointment_type

    def _status(self, patient: Patient, appointment_type: str, scheduled_at: datetime) -> str:
        r = self.rng.random()
        if scheduled_at > self.settings.now:
            return "cancelled" if r < FUTURE_CANCEL_RATE else "scheduled"
        payer_type = patient.payer.payer_type if patient.payer else None
        no_show = NO_SHOW_RATE.get(payer_type, NO_SHOW_RATE_DEFAULT)
        if appointment_type == "telehealth":
            no_show *= TELEHEALTH_NO_SHOW_FACTOR
        cancel = URGENT_CANCEL_RATE if appointment_type == "urgent" else CANCEL_RATE
        if r < no_show:
            return "no_show"
        if r < no_show + cancel:
            return "cancelled"
        return "completed"

    def _booked_at(self, patient: Patient, provider: Provider, appointment_type: str, scheduled_at: datetime) -> datetime:
        now = self.settings.now
        lead = timedelta(days=self.rng.uniform(*LEAD_DAYS[appointment_type]))
        if scheduled_at - lead > now:
            # A future appointment must already be booked "today".
            lead = (scheduled_at - now) + timedelta(days=self.rng.uniform(0, 14))
        earliest = max(patient.created_at, provider.hired_at)
        return min(max(scheduled_at - lead, earliest), now, scheduled_at)

    def build(self, scheduled_at: datetime, patient: Patient) -> Appointment | None:
        rng, now = self.rng, self.settings.now
        age = patient.age_on(scheduled_at.astimezone(CLINIC_TZ).date())

        specialty, provider = self._specialty_and_provider(patient, age, scheduled_at)
        if provider is None:
            return None

        appointment_type = self._appointment_type(patient, specialty, scheduled_at)
        status = self._status(patient, appointment_type, scheduled_at)
        created_at = self._booked_at(patient, provider, appointment_type, scheduled_at)

        visit_start = visit_end = None
        if status == "completed":
            minutes = rng.uniform(*specialty.visit_minutes) * DURATION_FACTOR[appointment_type]
            visit_start = scheduled_at + timedelta(minutes=rng.triangular(-5, 25, 5))
            visit_end = min(visit_start + timedelta(minutes=minutes), now)
            updated_at = min(visit_end + timedelta(minutes=rng.uniform(1, 20)), now)
        elif status == "no_show":
            updated_at = min(scheduled_at + timedelta(minutes=rng.uniform(20, 180)), now)
        elif status == "cancelled":
            cancel_by = min(scheduled_at, now)
            updated_at = created_at + (cancel_by - created_at) * rng.random()
        else:  # scheduled
            updated_at = created_at

        key = (patient.id, specialty.name)
        if status in ("completed", "scheduled"):
            self.history.seen_specialties.add(key)
            seen = self.history.specialists_seen.setdefault(patient.id, [])
            if specialty.name not in PRIMARY_CARE and specialty.name not in seen:
                seen.append(specialty.name)
        if status == "completed" and appointment_type == "annual_wellness":
            self.history.last_wellness[patient.id] = scheduled_at

        return Appointment(
            id=uuid7(created_at, rng),
            patient=patient,
            provider=provider,
            scheduled_at=scheduled_at,
            status=status,
            appointment_type=appointment_type,
            created_at=created_at,
            updated_at=updated_at,
            visit_start=visit_start,
            visit_end=visit_end,
        )


def generate(settings: SeedSettings, providers: list[Provider], patients: list[Patient]) -> list[Appointment]:
    rng = settings.rng("appointments")
    visits = _sample_visits(rng, settings, patients)
    visits.sort()  # chronological; ties broken by patient index, so the order is deterministic

    generator = _Generator(settings, providers, rng)
    appointments = [
        appointment
        for scheduled_at, index in visits
        if (appointment := generator.build(scheduled_at, patients[index])) is not None
    ]
    # Insert in booking order, matching the UUIDv7 order (append-mostly primary key index).
    appointments.sort(key=lambda a: a.created_at)
    return appointments


def seed(
    conn: psycopg.Connection, settings: SeedSettings, providers: list[Provider], patients: list[Patient]
) -> list[Appointment]:
    """Insert appointments and return them (with visit timing) for the encounter seeder."""
    appointments = generate(settings, providers, patients)
    copy_rows(
        conn,
        "appointments",
        COLUMNS,
        (
            (a.id, a.patient.id, a.provider.id, a.location.id, a.scheduled_at, a.status,
             a.appointment_type, a.created_at, a.updated_at)
            for a in appointments
        ),
    )
    return appointments


def main() -> None:
    settings = load_settings()
    with connect() as conn:
        locations = locations_seed.seed(conn, settings)
        payers = payers_seed.seed(conn, settings)
        providers = providers_seed.seed(conn, settings, locations)
        patients = patients_seed.seed(conn, settings, locations, payers)
        appointments = seed(conn, settings, providers, patients)
        print(f"Inserted {len(appointments)} appointments (preview, will roll back)\n")

        print("By status:")
        for status, count, share in conn.execute(
            """
            select status, count(*), round(100.0 * count(*) / sum(count(*)) over (), 1)
            from appointments group by status order by count(*) desc
            """
        ):
            print(f"  {status:<10} {count:>6}  {share:>5}%")

        print("\nBy type:")
        for appointment_type, count in conn.execute(
            "select appointment_type, count(*) from appointments group by 1 order by 2 desc"
        ):
            print(f"  {appointment_type:<16} {count:>6}")

        print("\nBy quarter and clinic (scheduled_at):")
        rows = conn.execute(
            """
            select to_char(date_trunc('quarter', a.scheduled_at at time zone 'America/Denver'), 'YYYY-"Q"Q'),
                   l.city, count(*)
            from appointments a join locations l on l.id = a.location_id
            group by 1, 2, l.created_at order by 1, l.created_at
            """
        ).fetchall()
        cities = list(dict.fromkeys(city for _, city, _ in rows))
        table: dict[str, dict[str, int]] = {}
        for quarter, city, count in rows:
            table.setdefault(quarter, {})[city] = count
        print("  quarter  " + "".join(f"{c[:10]:>11}" for c in cities))
        for quarter, counts in table.items():
            print(f"  {quarter}  " + "".join(f"{counts.get(c, 0):>11}" for c in cities))

        print("\nAppointments per provider (whole window):")
        low, median, high = conn.execute(
            """
            select min(n), percentile_disc(0.5) within group (order by n), max(n)
            from (select count(*) n from appointments group by provider_id) t
            """
        ).fetchone()
        print(f"  min {low}, median {median}, max {high}")

        print("\nChecks (all should be 0):")
        checks = {
            "scheduled in the past / resolved in the future": """
                select count(*) from appointments
                where (status = 'scheduled') <> (scheduled_at > %(now)s)
                  and not (status = 'cancelled' and scheduled_at > %(now)s)""",
            "location differs from provider's clinic": """
                select count(*) from appointments a join providers p on p.id = a.provider_id
                where a.location_id <> p.location_id""",
            "booked before patient registered or provider hired": """
                select count(*) from appointments a
                join patients pt on pt.id = a.patient_id join providers p on p.id = a.provider_id
                where a.created_at < pt.created_at or a.created_at < p.created_at""",
            "scheduled after provider left": """
                select count(*) from appointments a join providers p on p.id = a.provider_id
                where not p.active and a.scheduled_at >= p.updated_at""",
            "booked after scheduled time or after now": """
                select count(*) from appointments where created_at > scheduled_at or created_at > %(now)s""",
            "on a Sunday (clinic time)": """
                select count(*) from appointments
                where extract(isodow from scheduled_at at time zone 'America/Denver') = 7""",
            "adult seen in pediatrics": """
                select count(*) from appointments a
                join patients pt on pt.id = a.patient_id join providers p on p.id = a.provider_id
                where p.specialty = 'Pediatrics'
                  and extract(year from age((a.scheduled_at at time zone 'America/Denver')::date, pt.date_of_birth)) >= 18""",
            "repeat new_patient visit with same specialty": """
                select count(*) from (
                    select a.patient_id, p.specialty from appointments a join providers p on p.id = a.provider_id
                    where a.appointment_type = 'new_patient' and a.status = 'completed'
                    group by 1, 2 having count(*) > 1) t""",
        }
        for label, query in checks.items():
            count = conn.execute(query, {"now": settings.now}).fetchone()[0]
            print(f"  {count:>4}  {label}")

        conn.rollback()


if __name__ == "__main__":
    main()
