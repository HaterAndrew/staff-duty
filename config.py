"""
Input configuration dataclasses for the staff duty roster solver.

Each quarter, the caller supplies:
  - A list of directorates with eligible soldier counts
  - The quarter date range
  - The role label (SDNCO or SD_Runner)

Two separate RosterConfig objects are created and solved independently —
one for SDNCO positions and one for SD Runner positions.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import List


@dataclass
class Directorate:
    """A single directorate (e.g. G1, G3) and its eligible headcount."""
    name: str           # Display name: "G1", "G3", "ACOS", etc.
    eligible: int       # Number of soldiers eligible to pull this duty

    def __post_init__(self):
        if self.eligible < 1:
            raise ValueError(f"Directorate {self.name} must have at least 1 eligible soldier.")


@dataclass
class RosterConfig:
    """
    Full configuration for one quarterly roster run.

    role        : "SDNCO" or "SD_Runner" — used in output labeling only.
    start_date  : First day of the quarter (inclusive).
    end_date    : Last day of the quarter (inclusive).
    directorates: List of Directorate objects.
    """
    role: str
    start_date: date
    end_date: date
    directorates: List[Directorate] = field(default_factory=list)

    def __post_init__(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date.")
        if not self.directorates:
            raise ValueError("At least one directorate is required.")
        valid_roles = {"SDNCO", "SD_Runner"}
        if self.role not in valid_roles:
            raise ValueError(f"role must be one of {valid_roles}.")

    @property
    def total_eligible(self) -> int:
        return sum(d.eligible for d in self.directorates)

    @property
    def n_days(self) -> int:
        return (self.end_date - self.start_date).days + 1
