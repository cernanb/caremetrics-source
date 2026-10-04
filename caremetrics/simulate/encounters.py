"""Late chart amendments on encounters.

After a visit, some charts are amended days later (late documentation, addenda): the
seed's share and delay (caremetrics.seed.encounters). The schema has no note columns,
so an amendment shows up only as a new updated_at on the encounter. That is exactly
what a cursor-based sync or a dbt snapshot sees when a chart changes.

Whether and when an encounter is amended comes from its own random stream, and an
amendment is applied by the run whose window contains its moment, so it happens
exactly once however the runs are split. Encounters completed before the seed's
anchor are left alone; the seed already decided their amendments.
"""

from datetime import timedelta

import psycopg

from caremetrics.seed.config import SeedSettings
from caremetrics.seed.encounters import AMENDED_DAYS, AMENDED_SHARE
from caremetrics.simulate.core import SimulationError, Window, entity_rng


def amend(conn: psycopg.Connection, settings: SeedSettings, window: Window) -> dict[str, int]:
    recent = conn.execute(
        "select id, completed_at from encounters where completed_at > %s and completed_at > %s",
        (settings.now, window.start - timedelta(days=AMENDED_DAYS[1])),
    ).fetchall()

    amended = []
    for encounter_id, completed_at in recent:
        rng = entity_rng(settings, "encounter-amendment", encounter_id)
        is_amended = rng.random() < AMENDED_SHARE
        amended_at = completed_at + timedelta(days=rng.uniform(*AMENDED_DAYS))
        if is_amended and window.start < amended_at <= window.end:
            amended.append(encounter_id)

    if not amended:
        return {}
    with conn.cursor() as cur:
        # The updated_at trigger stamps the run time; nothing else about the row changes.
        cur.execute("update encounters set updated_at = now() where id = any(%s)", (amended,))
        if cur.rowcount != len(amended):
            raise SimulationError(f"expected to amend {len(amended)} encounters, amended {cur.rowcount}")
    return {"encounters_amended": len(amended)}
