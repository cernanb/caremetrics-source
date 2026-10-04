"""Types shared by the simulator command and its steps."""

from dataclasses import dataclass
from datetime import datetime


class SimulationError(Exception):
    """A condition that makes running the simulator unsafe; the run is rolled back."""


@dataclass(frozen=True)
class Window:
    """The span of simulated time one run covers: (start, end]."""

    start: datetime  # exclusive: end of the previous run, or the seed anchor
    end: datetime    # inclusive: this run's "now"

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M:%S%z} -> {self.end:%Y-%m-%d %H:%M:%S%z} ({self.end - self.start})"
