"""Types and helpers shared by the simulator command and its steps."""

import random
from dataclasses import dataclass
from datetime import datetime

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