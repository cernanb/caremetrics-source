"""Book new appointments made inside the run's window.

Every simulated day has a booking volume proportional to the total utilization of
the patients registered by then (calibrated to the seed's recent rate of about 116
bookings a day), with more bookings on weekdays than at weekends. A day's count and
booking times come from that day's own random stream, and each booking's details
from its own stream, so the bookings never depend on how often the simulator runs.

Each booking follows the seed's rules (caremetrics.seed.appointments):

* who:       a registered patient, chosen in proportion to utilization. In addition,
             every patient the simulator registers books a first primary care visit
             (new_patient) within half an hour of registering.
* specialty: mostly primary care (pediatrics or family medicine for children, family
             or internal medicine for adults), with an age-dependent specialist share;
             patients usually return to specialists they already see; specialists only
             see eligible patients
* type:      a patient's first visit with a specialty is new_patient (for primary care
             only if the patient joined during the history window); annual wellness at
             most about once a year
* provider:  employed on the booking day and on the visit day; usually the provider
             the patient saw last time, otherwise one at the patient's home clinic,
             favouring smaller panels
* when:      the seed's lead time for the type, on a day the clinic is open (urgent
             visits may use the light Saturday schedule), in a 15-minute slot

History-dependent rules look at the patient's visits as they stood at the moment of
booking. Bookings are processed in time order within a run, and an appointment that
is still scheduled is judged by the fate appointments.decide() gives it (for example,
whether it was cancelled before this booking was made). That makes a booking depend
only on what happened before it, not on how simulated time was split into runs.
"""

import uuid
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import accumulate
from types import SimpleNamespace

import psycopg
from psycopg.rows import namedtuple_row

from caremetrics.db import copy_rows
from caremetrics.seed.appointments import (
    ADULT_FAMILY_MEDICINE_SHARE,
    CHILD_PEDIATRICS_SHARE,
    CLINIC_TZ,
    HOUR_WEIGHTS,
    LEAD_DAYS,
    OBGYN_WEIGHT_BOOST,
    PRIMARY_CARE,
    PROVIDER_CONTINUITY,
    SPECIALIST_RETURN,
    SPECIALIST_SHARE_BY_AGE,
    WELLNESS_MIN_GAP,
    clinic_holidays,
)
from caremetrics.seed.config import SeedSettings, uuid7
from caremetrics.seed.providers import SPECIALTIES, SpecialtyProfile
from caremetrics.simulate.appointments import decide
from caremetrics.simulate.core import Window, daily_count, entity_rng, local_days, times_on

SPECIALTY_BY_NAME = {s.name: s for s in SPECIALTIES}

# Bookings per day per unit of registered-patient utilization: the seed's ~116 bookings
# a day over its ~11,978 units of utilization at the anchor.
BOOKINGS_PER_UTILIZATION_DAY = 116 / 11_978
# Relative booking volume by weekday (Monday = 0); averages to 1.0 over a week.
WEEKDAY_FACTOR = (1.2, 1.2, 1.2, 1.2, 1.2, 0.6, 0.4)
# When bookings are made (phone and online), local hour -> relative weight.
BOOKING_HOUR_WEIGHTS = {7: 0.5, 8: 1.0, 9: 1.0, 10: 1.0, 11: 1.0, 12: 0.8, 13: 1.0,
                        14: 1.0, 15: 1.0, 16: 0.9, 17: 0.6, 18: 0.4, 19: 0.3}
# The earliest slot a booking can take, relative to the moment it is made.
MIN_NOTICE = timedelta(minutes=30)
# A newly registered patient books their first visit this many minutes after registering.
FIRST_VISIT_MINUTES = (1, 30)

PATIENTS = """
    select pt.id, pt.date_of_birth, pt.gender, pt.created_at,
           pp.home_location_id, pp.utilization, py.payer_type
    from patients pt
    join simulator.patient_profiles pp on pp.patient_id = pt.id
    left join payers py on py.id = pp.payer_id
    order by pt.created_at, pt.id
"""

PROVIDERS = """
    select id, specialty, location_id, created_at as hired_at,
           case when active then null else updated_at end as departed_at
    from providers
"""

APPOINTMENT_HISTORY = """
    select a.id, a.patient_id, a.provider_id, a.scheduled_at, a.status, a.appointment_type,
           a.created_at, p.specialty, py.payer_type
    from appointments a
    join providers p on p.id = a.provider_id
    left join simulator.patient_profiles pp on pp.patient_id = a.patient_id
    left join payers py on py.id = pp.payer_id
"""

APPOINTMENT_COLUMNS = ("id", "patient_id", "provider_id", "location_id", "scheduled_at", "status",
                       "appointment_type", "created_at", "updated_at")


@dataclass(frozen=True, slots=True)
class Visit:
    """An appointment as the booking rules see it."""

    provider_id: uuid.UUID
    specialty: str
    appointment_type: str
    scheduled_at: datetime
    created_at: datetime
    fate: str                 # the status it has or will end with
    fate_at: datetime | None  # when a cancellation/no-show takes effect; None if before this run

    def kept_at(self, moment: datetime) -> bool:
        """Booked by `moment` and not cancelled or missed by then."""
        if self.created_at > moment:
            return False
        lapsed = self.fate in ("cancelled", "no_show") and (self.fate_at is None or self.fate_at <= moment)
        return not lapsed


def _age_on(date_of_birth: date, day: date) -> int:
    return day.year - date_of_birth.year - ((day.month, day.day) < (date_of_birth.month, date_of_birth.day))


def _eligible(specialty: SpecialtyProfile, patient, age: int) -> bool:
    low, high = specialty.patient_age
    return low <= age <= high and (not specialty.female_only or patient.gender == "female")


def _weighted(rng, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


class _Booker:
    def __init__(self, settings: SeedSettings, window: Window, patients, providers, history) -> None:
        self.settings = settings
        self.window = window
        self.patients = patients
        self.registered_at = [p.created_at for p in patients]
        self.cumulative_utilization = list(accumulate(p.utilization for p in patients))
        self.providers_by_specialty: dict[str, list] = {s.name: [] for s in SPECIALTIES}
        for provider in providers:
            self.providers_by_specialty[provider.specialty].append(provider)
        self.provider_by_id = {p.id: p for p in providers}

        self.visits: dict[uuid.UUID, list[Visit]] = {}
        self.panel: dict[uuid.UUID, set[uuid.UUID]] = {p.id: set() for p in providers}
        for row in history:
            if row.status == "scheduled":
                outcome = decide(settings, row)
                fate, fate_at = outcome.status, outcome.happens_at
            else:
                fate, fate_at = row.status, None
            self._remember(row.patient_id, Visit(row.provider_id, row.specialty, row.appointment_type,
                                                 row.scheduled_at, row.created_at, fate, fate_at))

    def _remember(self, patient_id: uuid.UUID, visit: Visit) -> None:
        self.visits.setdefault(patient_id, []).append(visit)
        self.panel[visit.provider_id].add(patient_id)

    # -- volume ------------------------------------------------------------------

    def utilization_registered_by(self, moment: datetime) -> float:
        count = bisect_right(self.registered_at, moment)
        return self.cumulative_utilization[count - 1] if count else 0.0

    def booking_times(self, day: date) -> list[datetime]:
        rng = entity_rng(self.settings, "bookings-day", day.isoformat())
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=CLINIC_TZ)
        mean = (BOOKINGS_PER_UTILIZATION_DAY * self.utilization_registered_by(day_end)
                * WEEKDAY_FACTOR[day.weekday()])
        return times_on(rng, day, CLINIC_TZ, BOOKING_HOUR_WEIGHTS, daily_count(rng, mean))

    # -- one booking ---------------------------------------------------------------

    def _patient(self, rng, booked_at: datetime):
        count = bisect_right(self.registered_at, booked_at)
        if count == 0:
            return None
        target = rng.random() * self.cumulative_utilization[count - 1]
        return self.patients[min(bisect_right(self.cumulative_utilization, target, 0, count), count - 1)]

    def _kept(self, patient, moment: datetime) -> list[Visit]:
        return [v for v in self.visits.get(patient.id, []) if v.kept_at(moment)]

    def _primary_specialty(self, patient, age: int, kept: list[Visit]) -> SpecialtyProfile:
        child = age < 18
        group = ("Pediatrics", "Family Medicine") if child else ("Family Medicine", "Internal Medicine")
        previous = [v for v in kept if v.specialty in group]
        if previous:
            return SPECIALTY_BY_NAME[max(previous, key=lambda v: v.scheduled_at).specialty]
        default_rng = entity_rng(self.settings, "primary-specialty", f"{patient.id}:{child}")
        share = CHILD_PEDIATRICS_SHARE if child else ADULT_FAMILY_MEDICINE_SHARE
        return SPECIALTY_BY_NAME[group[0] if default_rng.random() < share else group[1]]

    def _specialist(self, rng, patient, age: int, kept: list[Visit]) -> SpecialtyProfile | None:
        seen = sorted({v.specialty for v in kept if v.specialty not in PRIMARY_CARE
                       and _eligible(SPECIALTY_BY_NAME[v.specialty], patient, age)})
        if seen and rng.random() < SPECIALIST_RETURN:
            return SPECIALTY_BY_NAME[rng.choice(seen)]
        weights = {s.name: s.headcount * (OBGYN_WEIGHT_BOOST if s.female_only else 1.0)
                   for s in SPECIALTIES if s.name not in PRIMARY_CARE and _eligible(s, patient, age)}
        return SPECIALTY_BY_NAME[_weighted(rng, weights)] if weights else None

    def _appointment_type(self, rng, patient, specialty: SpecialtyProfile, kept: list[Visit]) -> str:
        seen = any(v.specialty == specialty.name for v in kept)
        if not seen and (specialty.name not in PRIMARY_CARE or patient.created_at >= self.settings.history_start):
            return "new_patient"
        return _weighted(rng, {t: w for t, w in specialty.appointment_types.items() if t != "new_patient"})

    def _slot(self, rng, appointment_type: str, booked_at: datetime) -> datetime:
        target = booked_at + timedelta(days=rng.uniform(*LEAD_DAYS[appointment_type]))
        day = target.astimezone(CLINIC_TZ).date()
        while True:
            closed = day.weekday() == 6 or day in clinic_holidays(day.year)
            saturday_not_allowed = day.weekday() == 5 and appointment_type != "urgent"
            if not (closed or saturday_not_allowed):
                hour = rng.choices(list(HOUR_WEIGHTS), weights=list(HOUR_WEIGHTS.values()))[0]
                local = datetime.combine(day, time(hour, rng.choice((0, 15, 30, 45))), tzinfo=CLINIC_TZ)
                slot = local.astimezone(timezone.utc)
                if slot >= booked_at + MIN_NOTICE:
                    return slot
            day += timedelta(days=1)

    def _provider(self, rng, patient, specialty: SpecialtyProfile, kept: list[Visit],
                  booked_at: datetime, scheduled_at: datetime):
        candidates = [
            p for p in self.providers_by_specialty[specialty.name]
            if p.hired_at <= booked_at and (p.departed_at is None or scheduled_at < p.departed_at)
        ]
        if not candidates:
            return None
        previous = [v for v in kept if v.specialty == specialty.name]
        if previous:
            preferred = self.provider_by_id[max(previous, key=lambda v: v.scheduled_at).provider_id]
            if preferred in candidates and rng.random() < PROVIDER_CONTINUITY:
                return preferred
        nearby = [p for p in candidates if p.location_id == patient.home_location_id] or candidates
        return rng.choices(nearby, weights=[1 / (1 + len(self.panel[p.id])) ** 2 for p in nearby])[0]

    def book(self, key: str, booked_at: datetime, patient=None):
        """One booking made at `booked_at`, or None if no suitable provider is employed.

        With `patient` given, this is that patient's first visit: always primary care.
        """
        rng = entity_rng(self.settings, "booking", key)
        first_visit = patient is not None
        patient = patient or self._patient(rng, booked_at)
        if patient is None:
            return None
        kept = self._kept(patient, booked_at)
        age = _age_on(patient.date_of_birth, booked_at.astimezone(CLINIC_TZ).date())

        share = next(s for upper, s in SPECIALIST_SHARE_BY_AGE if age < upper)
        specialty = None
        if not first_visit and rng.random() < share:
            specialty = self._specialist(rng, patient, age, kept)
        specialty = specialty or self._primary_specialty(patient, age, kept)
        appointment_type = self._appointment_type(rng, patient, specialty, kept)
        scheduled_at = self._slot(rng, appointment_type, booked_at)

        # The patient may age out of the specialty by the visit day (a 17-year-old in
        # pediatrics turning 18): family medicine sees everyone.
        age_at_visit = _age_on(patient.date_of_birth, scheduled_at.astimezone(CLINIC_TZ).date())
        provider = None
        if _eligible(specialty, patient, age_at_visit):
            provider = self._provider(rng, patient, specialty, kept, booked_at, scheduled_at)
        if provider is None and specialty.name != "Family Medicine":
            specialty = SPECIALTY_BY_NAME["Family Medicine"]
            appointment_type = self._appointment_type(rng, patient, specialty, kept)
            provider = self._provider(rng, patient, specialty, kept, booked_at, scheduled_at)
        if provider is None:
            return None

        if appointment_type == "annual_wellness" and any(
            v.appointment_type == "annual_wellness" and v.fate == "completed"
            and abs(v.scheduled_at - scheduled_at) < WELLNESS_MIN_GAP
            for v in kept
        ):
            appointment_type = "follow_up"

        appointment_id = uuid7(booked_at, rng)
        outcome = decide(self.settings, SimpleNamespace(
            id=appointment_id, payer_type=patient.payer_type, appointment_type=appointment_type,
            created_at=booked_at, scheduled_at=scheduled_at, specialty=specialty.name,
        ))
        self._remember(patient.id, Visit(provider.id, specialty.name, appointment_type, scheduled_at,
                                         booked_at, outcome.status, outcome.happens_at))
        return (appointment_id, patient.id, provider.id, provider.location_id, scheduled_at, "scheduled",
                appointment_type, booked_at, self.window.end)


def book(conn: psycopg.Connection, settings: SeedSettings, window: Window) -> dict[str, int]:
    """Insert the bookings made in (window.start, window.end]."""
    with conn.cursor(row_factory=namedtuple_row) as cur:
        patients = cur.execute(PATIENTS).fetchall()
        providers = cur.execute(PROVIDERS).fetchall()
        history = cur.execute(APPOINTMENT_HISTORY).fetchall()
    booker = _Booker(settings, window, patients, providers, history)

    # (booked_at, key, patient or None) for every booking made inside the window.
    # Earlier runs already made the ones at or before window.start.
    events = []
    for day in local_days(window, CLINIC_TZ):
        for index, booked_at in enumerate(booker.booking_times(day)):
            if window.start < booked_at <= window.end:
                events.append((booked_at, f"{day.isoformat()}:{index}", None))
    for patient in patients:
        if patient.created_at > settings.now:  # registered by the simulator
            rng = entity_rng(settings, "first-visit", patient.id)
            booked_at = patient.created_at + timedelta(minutes=rng.uniform(*FIRST_VISIT_MINUTES))
            if window.start < booked_at <= window.end:
                events.append((booked_at, f"first-visit:{patient.id}", patient))

    # Chronological order, so each booking sees every booking made before it.
    events.sort(key=lambda event: (event[0], event[1]))
    bookings = [row for booked_at, key, patient in events
                if (row := booker.book(key, booked_at, patient)) is not None]

    booked = copy_rows(conn, "appointments", APPOINTMENT_COLUMNS, bookings)
    return {"appointments_booked": booked} if booked else {}
