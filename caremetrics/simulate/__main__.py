"""Advance the operational database through simulated time.

    python -m caremetrics.simulate                        # catch up to now and commit
    python -m caremetrics.simulate --dry-run              # show what would change, then roll back
    python -m caremetrics.simulate --until 2026-10-03T12:00Z   # stop simulated time earlier than now

See caremetrics/simulate/runner.py for how a run works and what it guarantees.
"""

import argparse
import sys
from datetime import datetime, timezone

from caremetrics.db import connect
from caremetrics.seed.config import load_settings
from caremetrics.simulate.core import SimulationError
from caremetrics.simulate.runner import run


def _instant(value: str) -> datetime:
    """ISO 8601 date-time; without an offset it is taken as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 date-time: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m caremetrics.simulate",
        description="Advance the CareMetrics operational data through simulated time.",
    )
    parser.add_argument("--dry-run", action="store_true", help="report changes, then roll back")
    parser.add_argument(
        "--until", type=_instant, metavar="DATETIME",
        help="end simulated time at this instant instead of now (ISO 8601; UTC if no offset; not in the future)",
    )
    args = parser.parse_args()
    settings = load_settings()

    with connect() as conn:
        try:
            result = run(conn, settings, args.until)
        except SimulationError as exc:
            # Leaving the `with` block via sys.exit rolls the transaction back.
            sys.exit(f"Simulation aborted: {exc}")

        if result is None:
            last = conn.execute("select max(simulated_until) from simulator.runs").fetchone()[0]
            print(f"Nothing to simulate: simulated time already reaches {last or settings.now:%Y-%m-%d %H:%M:%S%z}.")
            return

        print(f"Simulating {result.window}")
        if result.profiles_bootstrapped:
            print(f"  bootstrapped {result.profiles_bootstrapped:,} patient profiles")
        for name, counts in result.step_counts:
            summary = ", ".join(f"{k}={v:,}" for k, v in counts.items()) or "no changes"
            print(f"  {name:<13} {summary}")

        if args.dry_run:
            conn.rollback()
            print("Dry run: all changes rolled back.")
            return
    # Leaving the `with` block committed the transaction.
    print(f"Committed run {result.run_id}.")


if __name__ == "__main__":
    main()
