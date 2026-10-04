"""Shared settings and deterministic randomness for the seed modules.

Reproducibility rules every seed module follows:

* Never use the global `random` module or an unseeded Faker. Ask SeedSettings for a
  named stream instead: settings.rng("patients"), settings.faker("patients").
  Each stream is seeded from (SEED_RANDOM_SEED, stream name), so changing how many
  random numbers one table consumes does not shift the values of any other table.

* Never use the wall clock. "Now" is SEED_ANCHOR_DATE (midnight UTC). The same seed
  and anchor produce the same rows, timestamps included, on every machine.

* IDs come from uuid7(created_at, rng): time-ordered like Postgres' uuidv7() default,
  but derived from the row's own created_at and a seeded stream, so they reproduce too.

Run directly to inspect settings and confirm determinism (run it twice, compare):

    python -m caremetrics.seed.config
"""

import os
import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from dotenv import load_dotenv
from faker import Faker

# Dataset shape. Locations and payers are fixed curated lists, so their counts
# live with those lists; encounters and claims follow from appointment outcomes.
PROVIDER_COUNT = 50
PATIENT_COUNT = 10_000
APPOINTMENT_COUNT = 75_000

HISTORY_DAYS = 730  # ~2 years of past activity before the anchor
FUTURE_DAYS = 60    # scheduled appointments up to ~60 days after the anchor

FAKER_LOCALE = "en_US"


@dataclass(frozen=True)
class SeedSettings:
    random_seed: int
    anchor_date: date

    @property
    def now(self) -> datetime:
        """The generator's notion of the current instant."""
        return datetime.combine(self.anchor_date, time.min, tzinfo=timezone.utc)

    @property
    def history_start(self) -> datetime:
        return self.now - timedelta(days=HISTORY_DAYS)

    @property
    def future_end(self) -> datetime:
        return self.now + timedelta(days=FUTURE_DAYS)

    def rng(self, stream: str) -> random.Random:
        # String seeds are hashed with SHA-512 by random.seed(), so this is stable
        # across processes and Python versions (unaffected by PYTHONHASHSEED).
        return random.Random(f"{self.random_seed}:{stream}")

    def faker(self, stream: str) -> Faker:
        fake = Faker(FAKER_LOCALE)
        fake.seed_instance(f"{self.random_seed}:{stream}:faker")
        return fake


def load_settings() -> SeedSettings:
    load_dotenv(override=False)

    raw_seed = os.environ.get("SEED_RANDOM_SEED", "42")
    raw_anchor = os.environ.get("SEED_ANCHOR_DATE", "2026-10-01")
    try:
        random_seed = int(raw_seed)
    except ValueError:
        raise ValueError(f"SEED_RANDOM_SEED must be an integer, got {raw_seed!r}") from None
    try:
        anchor_date = date.fromisoformat(raw_anchor)
    except ValueError:
        raise ValueError(f"SEED_ANCHOR_DATE must be YYYY-MM-DD, got {raw_anchor!r}") from None

    return SeedSettings(random_seed=random_seed, anchor_date=anchor_date)


def uuid7(created_at: datetime, rng: random.Random) -> uuid.UUID:
    """Deterministic RFC 9562 UUIDv7.

    Layout (128 bits): 48-bit Unix epoch milliseconds | 4-bit version (0b0111) |
    12 random bits | 2-bit variant (0b10) | 62 random bits.
    """
    unix_ms = int(created_at.timestamp() * 1000)
    rand_a = rng.getrandbits(12)
    rand_b = rng.getrandbits(62)
    value = (
        (unix_ms & 0xFFFF_FFFF_FFFF) << 80
        | 0x7 << 76
        | rand_a << 64
        | 0b10 << 62
        | rand_b
    )
    return uuid.UUID(int=value)


def main() -> None:
    settings = load_settings()
    print(f"random_seed:   {settings.random_seed}")
    print(f"now (anchor):  {settings.now.isoformat()}")
    print(f"history_start: {settings.history_start.isoformat()}")
    print(f"future_end:    {settings.future_end.isoformat()}")

    rng = settings.rng("demo")
    fake = settings.faker("demo")
    print(f"sample rng:    {rng.random():.6f}")
    print(f"sample name:   {fake.first_name()} {fake.last_name()}")
    print(f"sample uuid7:  {uuid7(settings.now, rng)}")


if __name__ == "__main__":
    main()