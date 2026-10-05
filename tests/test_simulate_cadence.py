"""Integration test: simulated outcomes do not depend on how often the simulator runs.

The same period of simulated time is processed as several runs on one fresh seed and
as a single run on another. Every table must end up identical apart from updated_at,
which records when rows were actually written.

This test RESEEDS the database. It only runs when DATABASE_URL points at localhost
and CAREMETRICS_TEST_RESETS_DB=1 is set:

    docker compose up -d
    CAREMETRICS_TEST_RESETS_DB=1 python -m unittest tests.test_simulate_cadence -v

Afterwards the database holds a fresh seed simulated to a fixed point (anchor + 3.5
days); `python -m caremetrics.simulate` then catches it up to now.
"""

import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from caremetrics.db import connect, database_url
from caremetrics.seed.config import load_settings
from caremetrics.simulate.runner import run

VERIFY_SQL = Path(__file__).resolve().parent.parent / "sql" / "verify_data.sql"

# Rows compared between runs: every column that carries simulated meaning (not updated_at).
FINGERPRINTS = {
    "patients": "concat_ws('|', id, first_name, last_name, date_of_birth, gender, state, created_at)",
    "appointments": "concat_ws('|', id, patient_id, provider_id, location_id, status, appointment_type, scheduled_at, created_at)",
    "encounters": "concat_ws('|', id, appointment_id, started_at, completed_at, created_at)",
    "claims": "concat_ws('|', id, encounter_id, payer_id, status, submitted_at, amount_billed, amount_paid, created_at)",
}


def _safe_to_reset() -> bool:
    host = conninfo_to_dict(database_url()).get("host")
    return os.environ.get("CAREMETRICS_TEST_RESETS_DB") == "1" and host in ("localhost", "127.0.0.1")


@unittest.skipUnless(_safe_to_reset(), "reseeds the database: needs a localhost DATABASE_URL and CAREMETRICS_TEST_RESETS_DB=1")
class CadenceTest(unittest.TestCase):
    settings = load_settings()
    anchor = settings.now
    # Deliberately awkward split points: mid-evening, overnight, mid-afternoon.
    splits = [anchor + timedelta(hours=19, minutes=17), anchor + timedelta(days=1, hours=3, minutes=17),
              anchor + timedelta(days=2, hours=15, minutes=41)]
    end = anchor + timedelta(days=3, hours=12)

    @staticmethod
    def _reseed() -> None:
        subprocess.run([sys.executable, "-m", "caremetrics.seed", "--reset"], check=True, capture_output=True)

    def _simulate(self, ends) -> None:
        for until in ends:
            with connect() as conn:
                self.assertIsNotNone(run(conn, self.settings, until))

    @staticmethod
    def _fingerprint() -> dict[str, str]:
        with connect() as conn:
            result = {
                table: conn.execute(f"select md5(string_agg({expr}, ',' order by id)) from {table}").fetchone()[0]
                for table, expr in FINGERPRINTS.items()
            }
            # Chart amendments and patient edits are UPDATEs, so the trigger stamps them with
            # the real clock time, which is after the simulated end point. Rows inserted by
            # the simulator carry their run's window end instead (never later than the end
            # point). So updated_at > end identifies exactly the amended and edited records.
            for table in ("encounters", "patients"):
                result[f"updated_{table}"] = conn.execute(
                    f"select md5(string_agg(id::text, ',' order by id)) from {table} where updated_at > %s",
                    (CadenceTest.end,),
                ).fetchone()[0]
            return result

    def test_split_runs_match_a_single_run(self):
        self._reseed()
        self._simulate([*self.splits, self.end])
        split = self._fingerprint()

        self._reseed()
        self._simulate([self.end])
        single = self._fingerprint()

        for key in split:
            with self.subTest(table=key):
                self.assertEqual(split[key], single[key])

    def test_rerun_is_a_no_op_and_data_stays_valid(self):
        self._reseed()
        self._simulate([self.end])
        with connect() as conn:
            self.assertIsNone(run(conn, self.settings, self.end))

        # verify_data.sql without its psql meta-commands; it raises if any check fails.
        script = "\n".join(line for line in VERIFY_SQL.read_text().splitlines() if not line.startswith("\\"))
        with connect(autocommit=True) as conn:
            conn.execute(script)


if __name__ == "__main__":
    unittest.main()
