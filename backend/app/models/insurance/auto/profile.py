"""The AUTO product-specific profile.

Composes drivers, vehicles, household, history, and coverage. Only AUTO is
implemented; see ``InsuranceProfile`` for the shared wrapper and the
unsupported-product mechanism.
"""

from __future__ import annotations

from pydantic import Field

from ..base import SensitiveBaseModel
from .coverage import CoverageConfiguration
from .driver import DriverInformation
from .history import InsuranceAndDrivingHistory
from .household import HouseholdInformation
from .vehicle import VehicleInformation


class AutoInsuranceProfile(SensitiveBaseModel):
    """Canonical Ontario private-passenger AUTO intake profile.

    ``drivers`` and ``vehicles`` require at least one entry each.
    """

    drivers: list[DriverInformation] = Field(default_factory=list, min_length=1)
    vehicles: list[VehicleInformation] = Field(default_factory=list, min_length=1)
    household: HouseholdInformation = Field(default_factory=HouseholdInformation)
    history: InsuranceAndDrivingHistory = Field(default_factory=InsuranceAndDrivingHistory)
    coverage: CoverageConfiguration = Field(default_factory=CoverageConfiguration)
