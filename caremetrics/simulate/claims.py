"""Create and advance insurance claims inside the run's window.

Two kinds of change, both decided from per-record random streams (core.entity_rng)
so the results never depend on how often the simulator runs:

1. Advance open claims (pending, submitted, accepted) along their timeline:

       created -> submitted -> adjudicated: denied | accepted -> paid

   The timeline uses the seed's delays and each payer's behaviour profile
   (caremetrics.seed.claims and caremetrics.seed.payers). A claim may move several
   steps in one run if the window is long.

   Transitions only move forward, and an accepted claim is never denied afterwards.
   Steps that already happened keep their stored dates (submitted_at, adjudicated_at);
   only the steps still ahead come from the timeline. A claim that was still open at
   the seed's anchor is known to have waited at least until the anchor, so its
   remaining delays are drawn on that condition (see timeline()): every new date falls
   after the anchor, and durations still follow the seed's delays and payer profiles.

2. Create claims for encounters completed after the seed's anchor that do not have
   one yet: insured patients only (payer from simulator.patient_profiles), minus the
   seed's share of non-billable visits. A claim exists once its charges are captured,
   a few hours to days after the visit; it is inserted already at the status its
   timeline has reached by the window's end.

Encounters completed before the anchor are left alone: the seed already decided which
of them were billed.

Timestamps: event times (created_at, submitted_at, adjudicated_at, paid_at) are
simulated; updated_at is the run time (trigger on UPDATE, window.end on INSERT).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import psycopg
from psycopg.rows import namedtuple_row

from caremetrics.db import copy_rows
from caremetrics.seed.claims import (
    CAPTURE_HOURS,
    CHARGE_FACTOR,
    CODING_QUERY_DAYS,
    CODING_QUERY_SHARE,
    NOT_BILLABLE_SHARE,
    PAYMENT_POSTING_DAYS,
    SUBMIT_DAYS,
)
from caremetrics.seed.config import SeedSettings, uuid7
from caremetrics.seed.payers import PROFILES, PayerProfile
from caremetrics.seed.providers import SPECIALTIES
from caremetrics.simulate.core import SimulationError, Window, entity_rng

PAYER_PROFILE_BY_NAME = {p.name: p for p in PROFILES}
SPECIALTY_BY_NAME = {s.name: s for s in SPECIALTIES}
CENT = Decimal("0.01")

# Position in the claim lifecycle; a claim only ever moves to a higher rank.
STATUS_RANK = {"pending": 0, "submitted": 1, "accepted": 2, "denied": 3, "paid": 3}

OPEN_CLAIMS = """
    select c.id, c.status, c.created_at, c.submitted_at, c.adjudicated_at, c.amount_billed,
           py.name as payer_name
    from claims c
    join payers py on py.id = c.payer_id
    where c.status in ('pending', 'submitted', 'accepted')
"""

UNBILLED_ENCOUNTERS = """
    select e.id, e.patient_id, e.provider_id, e.completed_at,
           a.appointment_type, p.specialty, pp.payer_id, py.name as payer_name
    from encounters e
    join appointments a on a.id = e.appointment_id
    join providers p on p.id = e.provider_id
    join simulator.patient_profiles pp on pp.patient_id = e.patient_id
    left join payers py on py.id = pp.payer_id
    where e.completed_at > %(anchor)s
      and not exists (select 1 from claims c where c.encounter_id = e.id)
"""

CLAIM_COLUMNS = ("id", "encounter_id", "patient_id", "provider_id", "payer_id", "submitted_at",
                 "adjudicated_at", "paid_at", "status", "amount_billed", "amount_paid",
                 "created_at", "updated_at")


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Timeline:
    submitted_at: datetime
    adjudicated_at: datetime
    denied: bool
    paid_at: datetime
    paid_ratio: float

    def status_at(self, moment: datetime, *, can_deny: bool = True) -> str:
        if self.submitted_at > moment:
            return "pending"
        if self.adjudicated_at > moment:
            return "submitted"
        if self.denied and can_deny:
            return "denied"
        if self.paid_at > moment:
            return "accepted"
        return "paid"

    def event_dates(self, status: str) -> tuple[datetime | None, datetime | None, datetime | None]:
        """(submitted_at, adjudicated_at, paid_at) as stored for a claim in this status:
        the dates of the events it has reached, None for the rest."""
        rank = STATUS_RANK[status]
        return (
            self.submitted_at if rank >= STATUS_RANK["submitted"] else None,
            self.adjudicated_at if rank >= STATUS_RANK["accepted"] else None,
            self.paid_at if status == "paid" else None,
        )

    def amount_paid(self, status: str, amount_billed: Decimal) -> Decimal:
        if status != "paid":
            return Decimal("0.00")
        return min(_money(float(amount_billed) * self.paid_ratio), amount_billed)


def _days_open_at_anchor(settings: SeedSettings, step_started: datetime) -> float:
    """How long a step that is still open had been waiting at the seed's anchor (0 if it
    started later). The seed stored every event that happened by its anchor, so a step
    the simulator still has to date was open at the anchor whenever it started before it."""
    return max(0.0, (settings.now - step_started) / timedelta(days=1))


def _share_longer_than(bounds: tuple[float, float], days: float) -> float:
    """Probability that a delay drawn uniformly from bounds (in days) exceeds days."""
    low, high = bounds
    return min(1.0, max(0.0, (high - max(low, days)) / (high - low)))


def _delay(bounds: tuple[float, float], fraction: float, at_least_days: float) -> timedelta:
    """A delay drawn uniformly from bounds (in days), given that it exceeds at_least_days.

    fraction is the random draw in [0, 1). With at_least_days at or below the lower bound
    this is exactly rng.uniform(*bounds); otherwise it is the same draw over the part of
    the range that is still possible.
    """
    low, high = bounds
    low = max(low, at_least_days)
    if low >= high:
        raise SimulationError(f"a delay in {bounds} days cannot exceed {at_least_days:.2f} days")
    return timedelta(days=low + (high - low) * fraction)


def timeline(
    settings: SeedSettings, claim_id: object, created_at: datetime,
    submitted_at: datetime | None, payer: PayerProfile,
    adjudicated_at: datetime | None = None,
) -> Timeline:
    """The claim's path through billing. Pure: same claim in, same timeline out.

    Dates the claim already has (submitted_at, adjudicated_at) are kept; the rest of
    the path is derived from them. A step that had already been waiting at the seed's
    anchor is drawn on the condition that it lasted past the anchor, so a claim the seed
    showed as open never gets a date before the anchor. For claims created after the
    anchor that condition is empty, and the draws are the plain uniform ones.
    """
    rng = entity_rng(settings, "claim-timeline", claim_id)
    # Draw every value up front, in a fixed order, so the stream never depends on the branch.
    hold_draw = rng.random()
    usual_fraction = rng.random()
    coding_fraction = rng.random()
    adjudication_fraction = rng.random()
    denied = rng.random() < payer.denial_rate
    posting_fraction = rng.random()
    paid_ratio = rng.uniform(*payer.paid_ratio)

    if submitted_at is None:
        # Submission is a mixture: usually days, sometimes a coding hold of weeks. Having
        # waited this long already makes a coding hold more likely (Bayes' rule); with no
        # wait, the probability is the plain CODING_QUERY_SHARE.
        waited = _days_open_at_anchor(settings, created_at)
        coding_weight = CODING_QUERY_SHARE * _share_longer_than(CODING_QUERY_DAYS, waited)
        usual_weight = (1 - CODING_QUERY_SHARE) * _share_longer_than(SUBMIT_DAYS, waited)
        if coding_weight + usual_weight == 0:
            raise SimulationError(f"claim {claim_id} could not still be pending after {waited:.2f} days")
        if hold_draw < coding_weight / (coding_weight + usual_weight):
            submitted_at = created_at + _delay(CODING_QUERY_DAYS, coding_fraction, waited)
        else:
            submitted_at = created_at + _delay(SUBMIT_DAYS, usual_fraction, waited)

    if adjudicated_at is None:
        waited = _days_open_at_anchor(settings, submitted_at)
        adjudicated_at = submitted_at + _delay(payer.days_to_adjudicate, adjudication_fraction, waited)

    waited = _days_open_at_anchor(settings, adjudicated_at)
    paid_at = adjudicated_at + _delay(PAYMENT_POSTING_DAYS, posting_fraction, waited)
    return Timeline(submitted_at, adjudicated_at, denied, paid_at, paid_ratio)


def _advance_open_claims(conn: psycopg.Connection, settings: SeedSettings, window: Window) -> dict[str, int]:
    with conn.cursor(row_factory=namedtuple_row) as cur:
        open_claims = cur.execute(OPEN_CLAIMS).fetchall()

    # (id, current status, new status, submitted_at, adjudicated_at, paid_at, amount_paid)
    changes = []
    for claim in open_claims:
        path = timeline(settings, claim.id, claim.created_at, claim.submitted_at,
                        PAYER_PROFILE_BY_NAME[claim.payer_name], claim.adjudicated_at)
        status = path.status_at(window.end, can_deny=claim.status != "accepted")
        if STATUS_RANK[status] <= STATUS_RANK[claim.status]:
            continue
        changes.append((claim.id, claim.status, status, *path.event_dates(status),
                        path.amount_paid(status, claim.amount_billed)))

    if not changes:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            update claims c
            set status = v.status, submitted_at = v.submitted_at, adjudicated_at = v.adjudicated_at,
                paid_at = v.paid_at, amount_paid = v.amount_paid
            from unnest(%s::uuid[], %s::text[], %s::text[], %s::timestamptz[], %s::timestamptz[],
                        %s::timestamptz[], %s::numeric[])
                 as v(id, current_status, status, submitted_at, adjudicated_at, paid_at, amount_paid)
            where c.id = v.id and c.status = v.current_status
            """,
            tuple(list(column) for column in zip(*changes)),
        )
        if cur.rowcount != len(changes):
            raise SimulationError(f"expected to update {len(changes)} claims, updated {cur.rowcount}")

    counts: dict[str, int] = {}
    for _, _, status, *_ in changes:
        counts[f"claims_now_{status}"] = counts.get(f"claims_now_{status}", 0) + 1
    return counts


def _create_claims(conn: psycopg.Connection, settings: SeedSettings, window: Window) -> int:
    with conn.cursor(row_factory=namedtuple_row) as cur:
        unbilled = cur.execute(UNBILLED_ENCOUNTERS, {"anchor": settings.now}).fetchall()

    rows = []
    for encounter in unbilled:
        rng = entity_rng(settings, "claim-new", encounter.id)
        billable = rng.random() >= NOT_BILLABLE_SHARE
        capture = timedelta(hours=rng.uniform(*CAPTURE_HOURS))
        charge_fraction = rng.random()
        if encounter.payer_id is None or not billable:
            continue  # self-pay patient, or a visit with nothing to bill
        created_at = encounter.completed_at + capture
        if created_at > window.end:
            continue  # charges not captured yet; a later run creates the claim

        low, high = SPECIALTY_BY_NAME[encounter.specialty].base_charge
        amount_billed = _money((low + (high - low) * charge_fraction) * CHARGE_FACTOR[encounter.appointment_type])
        claim_id = uuid7(created_at, entity_rng(settings, "claim-id", encounter.id))
        path = timeline(settings, claim_id, created_at, None, PAYER_PROFILE_BY_NAME[encounter.payer_name])
        status = path.status_at(window.end)
        rows.append((
            claim_id, encounter.id, encounter.patient_id, encounter.provider_id, encounter.payer_id,
            *path.event_dates(status), status,
            amount_billed, path.amount_paid(status, amount_billed), created_at, window.end,
        ))

    return copy_rows(conn, "claims", CLAIM_COLUMNS, rows)


def simulate(conn: psycopg.Connection, settings: SeedSettings, window: Window) -> dict[str, int]:
    """Advance open claims, then create claims for newly completed encounters."""
    counts = _advance_open_claims(conn, settings, window)
    created = _create_claims(conn, settings, window)
    if created:
        counts["claims_created"] = created
    return counts