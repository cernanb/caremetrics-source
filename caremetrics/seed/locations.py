"""Seed clinic locations.

Locations are curated reference data rather than Faker output: five clinics of one
fictional regional practice ("Larkspur Health") along Colorado's Front Range.
Real cities, invented organisation.

created_at is the clinic's go-live date in the operational system. Four clinics were
already operating before the history window; Colorado Springs North goes live inside
it, so its appointment volume starts (and ramps up) within the data. That gives later
analytics a realistic new-site trend instead of five identical flat lines.

Preview (inserts inside a transaction, prints, then rolls back):

    python -m caremetrics.seed.locations
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from caremetrics.db import connect, copy_rows
from caremetrics.seed.config import SeedSettings, load_settings, uuid7


@dataclass(frozen=True)
class Location:
    id: uuid.UUID
    name: str
    city: str
    state: str
    created_at: datetime


# (name, city, state, go-live). Times are ~9am Mountain expressed in UTC.
CLINICS: list[tuple[str, str, str, datetime]] = [
    ("Larkspur Health Capitol Hill Clinic",     "Denver",           "CO", datetime(2019, 3, 4, 16, 0, tzinfo=timezone.utc)),
    ("Larkspur Health Aurora Medical Plaza",    "Aurora",           "CO", datetime(2020, 8, 17, 15, 0, tzinfo=timezone.utc)),
    ("Larkspur Health Lakewood Family Clinic",  "Lakewood",         "CO", datetime(2021, 1, 11, 16, 0, tzinfo=timezone.utc)),
    ("Larkspur Health Boulder Valley Clinic",   "Boulder",          "CO", datetime(2022, 6, 6, 15, 0, tzinfo=timezone.utc)),
    ("Larkspur Health Colorado Springs North",  "Colorado Springs", "CO", datetime(2025, 4, 7, 15, 0, tzinfo=timezone.utc)),
]

COLUMNS = ("id", "name", "city", "state", "created_at", "updated_at")


def generate(settings: SeedSettings) -> list[Location]:
    rng = settings.rng("locations")
    # A clinic that goes live after the anchor date does not exist yet "today".
    # Only matters if SEED_ANCHOR_DATE is moved earlier than the default.
    return [
        Location(
            id=uuid7(go_live, rng),
            name=name,
            city=city,
            state=state,
            created_at=go_live,
        )
        for name, city, state, go_live in CLINICS
        if go_live <= settings.now
    ]


def seed(conn: psycopg.Connection, settings: SeedSettings) -> list[Location]:
    """Insert locations and return them for use by dependent seeders."""
    locations = generate(settings)
    copy_rows(
        conn,
        "locations",
        COLUMNS,
        # Reference rows have not been edited since creation, so updated_at = created_at.
        ((l.id, l.name, l.city, l.state, l.created_at, l.created_at) for l in locations),
    )
    return locations


def main() -> None:
    settings = load_settings()
    with connect() as conn:
        locations = seed(conn, settings)
        print(f"Inserted {len(locations)} locations (preview, will roll back):")
        rows = conn.execute(
            "select id, name, city, created_at::date from locations order by created_at"
        ).fetchall()
        for id_, name, city, go_live in rows:
            print(f"  {id_}  {go_live}  {name:<40} {city}")
        conn.rollback()


if __name__ == "__main__":
    main()