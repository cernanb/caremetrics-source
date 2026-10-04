"""Types and helpers shared by the simulator command and its steps."""

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from caremetrics.seed.config import SeedSettings


class SimulationError(Exception):
    """A condition that makes running the simulator unsafe; the run is rolled back."""


@dataclass(frozen=True)
class Window:
    """The span of simulated time one run covers: (start, end]."""

    start: datetime  # exclusive: end of the previous run, or the seed anchor
    end: datetime    # inclusive: this run's "now"

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M:%S%z} -> {self.end:%Y-%m-%d %H:%M:%S%z} ({self.end - self.start})"


def entity_rng(settings: SeedSettings, purpose: str, entity_id: object) -> random.Random:
    """A random stream belonging to one record and one purpose.

    Everything the simulator decides about a record (an appointment's outcome, a
    claim's timeline) is drawn from that record's own stream, so the result depends
    only on the record, never on how simulated time was split into runs.
    `purpose` keeps the streams for different decisions about the same record apart.
    """
    return random.Random(f"{settings.random_seed}:{purpose}:{entity_id}")

def local_days(window: Window, tz: tzinfo) -> list[date]:
    """Calendar days, in `tz`, that the window touches."""
    day, last = window.start.astimezone(tz).date(), window.end.astimezone(tz).date()
    days = []
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def times_on(rng: random.Random, day: date, tz: tzinfo, hour_weights: dict[int, float], count: int) -> list[datetime]:
    """`count` sorted UTC instants on `day`, with local hours drawn from `hour_weights`."""
    hours = rng.choices(list(hour_weights), weights=list(hour_weights.values()), k=count)
    return sorted(
        datetime.combine(day, time(hour), tzinfo=tz).astimezone(timezone.utc)
        + timedelta(seconds=rng.uniform(0, 3600))
        for hour in hours
    )


def daily_count(rng: random.Random, mean: float) -> int:
    """A day's event count around `mean` (normal approximation to Poisson)."""
    return max(0, round(rng.gauss(mean, math.sqrt(mean)))) if mean > 0 else 0
