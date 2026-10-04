"""Seed payers (insurance plans).

All payer names are invented. They are deliberately not real insurers, Medicare
Advantage plans, Medicaid programs or government health programs.

Each payer also carries generator-only behaviour (market share, denial rate,
reimbursement ratio, adjudication time). Those fields are NOT columns in the
database; the claims seeder uses them so payer differences show up naturally in
the data (for example, Medicaid paying a lower share of billed charges than commercial).

Preview (inserts inside a transaction, prints, then rolls back):

    python -m caremetrics.seed.payers
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from caremetrics.db import connect, copy_rows
from caremetrics.seed.config import SeedSettings, load_settings, uuid7


@dataclass(frozen=True)
class PayerProfile:
    name: str
    payer_type: str
    contracted_at: datetime  # becomes created_at
    # --- generator-only behaviour, not stored ---
    market_share: float               # relative weight among payers of the same payer_type
    denial_rate: float                # probability an adjudicated claim is denied
    paid_ratio: tuple[float, float]   # amount_paid / amount_billed range for paid claims
    days_to_adjudicate: tuple[int, int]  # submitted -> accepted/denied/paid


@dataclass(frozen=True)
class Payer:
    id: uuid.UUID
    profile: PayerProfile

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def payer_type(self) -> str:
        return self.profile.payer_type

    @property
    def created_at(self) -> datetime:
        return self.profile.contracted_at


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 17, 0, tzinfo=timezone.utc)


# Reimbursement ratios are against *billed charges*, which providers set well above
# what any payer actually pays. Government payers reimburse the lowest share.
PROFILES: list[PayerProfile] = [
    # commercial (~50% of covered patients)
    PayerProfile("Peakline Health Partners",       "commercial",       _utc(2019, 2, 1),  0.30, 0.07, (0.62, 0.82), (14, 35)),
    PayerProfile("Granite Shield Insurance",       "commercial",       _utc(2019, 2, 1),  0.25, 0.09, (0.58, 0.78), (18, 45)),
    PayerProfile("Cottonwood Mutual Health",       "commercial",       _utc(2020, 6, 15), 0.20, 0.06, (0.60, 0.80), (12, 30)),
    PayerProfile("Silverpine Health Assurance",    "commercial",       _utc(2021, 1, 4),  0.15, 0.11, (0.55, 0.75), (20, 50)),
    PayerProfile("Northgate Employer Health Trust", "commercial",      _utc(2022, 3, 1),  0.10, 0.05, (0.65, 0.85), (10, 28)),
    # medicare (Medicare Advantage-style plans; patients 65+)
    PayerProfile("Evergold Senior Advantage",      "medicare",         _utc(2019, 2, 1),  0.60, 0.05, (0.38, 0.55), (14, 30)),
    PayerProfile("Harborlight Medicare Plans",     "medicare",         _utc(2020, 1, 2),  0.40, 0.06, (0.36, 0.52), (16, 35)),
    # medicaid (managed Medicaid plans)
    PayerProfile("Prairie Commons Medicaid Plan",  "medicaid",         _utc(2019, 2, 1),  0.55, 0.12, (0.28, 0.45), (25, 60)),
    PayerProfile("Aspen Hollow Community Health",  "medicaid",         _utc(2021, 7, 1),  0.45, 0.10, (0.30, 0.46), (21, 55)),
    # other government (military / federal programs)
    PayerProfile("Frontier Service Members Health", "other_government", _utc(2019, 9, 3), 1.00, 0.04, (0.45, 0.60), (14, 30)),
]

COLUMNS = ("id", "name", "payer_type", "created_at", "updated_at")


def generate(settings: SeedSettings) -> list[Payer]:
    rng = settings.rng("payers")
    return [Payer(id=uuid7(p.contracted_at, rng), profile=p) for p in PROFILES]


def seed(conn: psycopg.Connection, settings: SeedSettings) -> list[Payer]:
    """Insert payers and return them (with behaviour profiles) for the claims seeder."""
    payers = generate(settings)
    copy_rows(
        conn,
        "payers",
        COLUMNS,
        ((p.id, p.name, p.payer_type, p.created_at, p.created_at) for p in payers),
    )
    return payers


def main() -> None:
    settings = load_settings()
    with connect() as conn:
        payers = seed(conn, settings)
        print(f"Inserted {len(payers)} payers (preview, will roll back):")
        rows = conn.execute(
            """
            select payer_type, name, created_at::date
            from payers
            order by payer_type, name
            """
        ).fetchall()
        for payer_type, name, contracted in rows:
            print(f"  {payer_type:<17} {name:<33} contracted {contracted}")
        conn.rollback()


if __name__ == "__main__":
    main()