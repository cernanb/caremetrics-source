"""Seed claims from encounters.

Which encounters get a claim:
* Uninsured (self-pay) patients: never. They are billed directly, outside claims.
* Insured patients: almost always, billed to the patient's payer. A small share of
  visits are not billable (post-operative global period, no-charge follow-ups).

Amounts:
* amount_billed = the specialty's standard charge x a visit-type multiplier
  (procedures and new-patient visits bill more, telehealth less).
* amount_paid is zero unless the claim is paid; a paid claim receives the payer's
  typical share of billed charges (see payers.PayerProfile.paid_ratio).

Lifecycle. Each claim gets a timeline, and its status is wherever that timeline
stands at "now" (the anchor date):

    encounter completed
      -> created_at    charges captured and coded (hours to a few days later)
      -> submitted_at  sent to the payer (days later; a few wait weeks on coding queries)
      -> adjudicated   payer decision after the payer's typical turnaround:
                       denied, or accepted
      -> paid          payment posted a few days to two weeks after acceptance

  status = pending   (not yet submitted)   | submitted (awaiting decision)
         | denied    (decision: denied)    | accepted  (approved, payment not posted)
         | paid

So old claims are resolved (paid or denied) while the most recent weeks still
show pending, submitted and accepted claims, as a live billing system would.
Each event that has happened by "now" is stored: submitted_at once submitted,
adjudicated_at once accepted or denied, paid_at once paid. updated_at is the time
of the latest transition.

Preview (seeds everything inside a transaction, prints, rolls back):

    python -m caremetrics.seed.claims
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import psycopg

from caremetrics.db import connect, copy_rows
from caremetrics.seed import appointments as appointments_seed
from caremetrics.seed import encounters as encounters_seed
from caremetrics.seed import locations as locations_seed
from caremetrics.seed import patients as patients_seed
from caremetrics.seed import payers as payers_seed
from caremetrics.seed import providers as providers_seed
from caremetrics.seed.config import SeedSettings, load_settings, uuid7
from caremetrics.seed.encounters import Encounter
from caremetrics.seed.payers import Payer

NOT_BILLABLE_SHARE = 0.03

CHARGE_FACTOR = {
    "procedure": 2.2, "consultation": 1.6, "new_patient": 1.5, "annual_wellness": 1.4,
    "urgent": 1.2, "follow_up": 1.0, "telehealth": 0.75,
}

CAPTURE_HOURS = (1, 72)           # encounter completed -> claim created
SUBMIT_DAYS = (0.1, 4)            # created -> submitted
CODING_QUERY_SHARE = 0.04         # claims held for coding review before submission
CODING_QUERY_DAYS = (10, 30)
PAYMENT_POSTING_DAYS = (3, 14)    # accepted -> paid

CENT = Decimal("0.01")

COLUMNS = ("id", "encounter_id", "patient_id", "provider_id", "payer_id", "submitted_at",
           "adjudicated_at", "paid_at", "status", "amount_billed", "amount_paid",
           "created_at", "updated_at")


@dataclass(frozen=True, slots=True)
class Claim:
    id: uuid.UUID
    encounter: Encounter
    payer: Payer
    submitted_at: datetime | None
    adjudicated_at: datetime | None
    paid_at: datetime | None
    status: str
    amount_billed: Decimal
    amount_paid: Decimal
    created_at: datetime
    updated_at: datetime


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _days(rng, bounds: tuple[float, float]) -> timedelta:
    return timedelta(days=rng.uniform(*bounds))


def generate(settings: SeedSettings, encounters: list[Encounter]) -> list[Claim]:
    rng = settings.rng("claims")
    now = settings.now
    claims: list[Claim] = []

    for encounter in encounters:
        appointment = encounter.appointment
        payer = appointment.patient.payer
        billable = rng.random() >= NOT_BILLABLE_SHARE  # always drawn, keeps the stream aligned
        if payer is None or not billable:
            continue

        profile = payer.profile
        specialty = appointment.provider.specialty
        amount_billed = _money(
            rng.uniform(*specialty.base_charge) * CHARGE_FACTOR[appointment.appointment_type]
        )

        created_at = min(
            encounter.completed_at + timedelta(hours=rng.uniform(*CAPTURE_HOURS)), now
        )
        if payer.created_at > created_at:
            raise ValueError(f"{payer.name} was not contracted yet at {created_at}")

        submit_delay = _days(rng, CODING_QUERY_DAYS if rng.random() < CODING_QUERY_SHARE else SUBMIT_DAYS)
        submitted_at = created_at + submit_delay
        adjudicated_at = submitted_at + _days(rng, profile.days_to_adjudicate)
        denied = rng.random() < profile.denial_rate
        paid_at = adjudicated_at + _days(rng, PAYMENT_POSTING_DAYS)
        paid_ratio = rng.uniform(*profile.paid_ratio)

        # Only events that have happened by now are stored; later ones stay null.
        amount_paid = Decimal("0.00")
        submitted = adjudicated = paid = None
        if submitted_at > now:
            status, updated_at = "pending", created_at
        elif adjudicated_at > now:
            status, updated_at = "submitted", submitted_at
            submitted = submitted_at
        elif denied:
            status, updated_at = "denied", adjudicated_at
            submitted, adjudicated = submitted_at, adjudicated_at
        elif paid_at > now:
            status, updated_at = "accepted", adjudicated_at
            submitted, adjudicated = submitted_at, adjudicated_at
        else:
            status, updated_at = "paid", paid_at
            submitted, adjudicated, paid = submitted_at, adjudicated_at, paid_at
            amount_paid = min(_money(float(amount_billed) * paid_ratio), amount_billed)

        claims.append(
            Claim(
                id=uuid7(created_at, rng),
                encounter=encounter,
                payer=payer,
                submitted_at=submitted,
                adjudicated_at=adjudicated,
                paid_at=paid,
                status=status,
                amount_billed=amount_billed,
                amount_paid=amount_paid,
                created_at=created_at,
                updated_at=updated_at,
            )
        )

    claims.sort(key=lambda c: c.created_at)
    return claims


def seed(conn: psycopg.Connection, settings: SeedSettings, encounters: list[Encounter]) -> list[Claim]:
    claims = generate(settings, encounters)
    copy_rows(
        conn,
        "claims",
        COLUMNS,
        (
            (c.id, c.encounter.id, c.encounter.appointment.patient.id,
             c.encounter.appointment.provider.id, c.payer.id, c.submitted_at, c.adjudicated_at,
             c.paid_at, c.status, c.amount_billed, c.amount_paid, c.created_at, c.updated_at)
            for c in claims
        ),
    )
    return claims


def main() -> None:
    settings = load_settings()
    with connect() as conn:
        locations = locations_seed.seed(conn, settings)
        payers = payers_seed.seed(conn, settings)
        providers = providers_seed.seed(conn, settings, locations)
        patients = patients_seed.seed(conn, settings, locations, payers)
        appointments = appointments_seed.seed(conn, settings, providers, patients)
        encounters = encounters_seed.seed(conn, settings, appointments)
        claims = seed(conn, settings, encounters)
        print(f"Inserted {len(claims)} claims for {len(encounters)} encounters (preview, will roll back)\n")

        print("By status:")
        for status, count, billed, paid in conn.execute(
            """
            select status, count(*), sum(amount_billed), sum(amount_paid)
            from claims group by status order by count(*) desc
            """
        ):
            print(f"  {status:<10} {count:>6}   billed ${billed:>13,.2f}   paid ${paid:>13,.2f}")

        print("\nBy payer type (adjudicated claims only):")
        for payer_type, count, denial_rate, paid_ratio, days_to_pay in conn.execute(
            """
            select py.payer_type, count(*),
                   round(100.0 * count(*) filter (where c.status = 'denied') / count(*), 1),
                   round(100 * avg(c.amount_paid / c.amount_billed) filter (where c.status = 'paid'), 1),
                   round(avg(extract(epoch from c.paid_at - c.submitted_at) / 86400)
                         filter (where c.status = 'paid'), 1)
            from claims c join payers py on py.id = c.payer_id
            where c.status in ('denied', 'accepted', 'paid')
            group by py.payer_type order by count(*) desc
            """
        ):
            print(f"  {payer_type:<17} {count:>6} claims   denied {denial_rate:>4}%   "
                  f"paid/billed {paid_ratio:>4}%   submit->paid {days_to_pay:>4} days")

        print("\nStatus mix by month of service (last 4 months):")
        for month, pending, submitted, accepted, denied, paid in conn.execute(
            """
            select to_char(date_trunc('month', e.completed_at at time zone 'America/Denver'), 'YYYY-MM'),
                   count(*) filter (where c.status = 'pending'),
                   count(*) filter (where c.status = 'submitted'),
                   count(*) filter (where c.status = 'accepted'),
                   count(*) filter (where c.status = 'denied'),
                   count(*) filter (where c.status = 'paid')
            from claims c join encounters e on e.id = c.encounter_id
            group by 1 order by 1 desc limit 4
            """
        ):
            print(f"  {month}  pending {pending:>4}  submitted {submitted:>5}  "
                  f"accepted {accepted:>4}  denied {denied:>4}  paid {paid:>5}")

        print("\nChecks (all should be 0):")
        checks = {
            "submitted before the encounter completed": """
                select count(*) from claims c join encounters e on e.id = c.encounter_id
                where c.submitted_at < e.completed_at""",
            "created before the encounter completed": """
                select count(*) from claims c join encounters e on e.id = c.encounter_id
                where c.created_at < e.completed_at""",
            "claim for a non-completed appointment": """
                select count(*) from claims c join encounters e on e.id = c.encounter_id
                join appointments a on a.id = e.appointment_id where a.status <> 'completed'""",
            "claim created before the payer was contracted": """
                select count(*) from claims c join payers py on py.id = c.payer_id
                where c.created_at < py.created_at""",
            "updated after now": """
                select count(*) from claims where updated_at > %(now)s or created_at > %(now)s""",
        }
        for label, query in checks.items():
            count = conn.execute(query, {"now": settings.now}).fetchone()[0]
            print(f"  {count:>4}  {label}")

        conn.rollback()


if __name__ == "__main__":
    main()
