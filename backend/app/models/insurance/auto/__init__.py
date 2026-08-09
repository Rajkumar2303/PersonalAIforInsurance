"""Auto insurance profile schema (Ontario private-passenger auto).

Issue #2: only AUTO is fully implemented. HOME/TENANT/LIFE/TRAVEL/OTHER are
recognized by ``InsuranceType`` but unsupported (see ``InsuranceProfile``).
"""

from __future__ import annotations

from .coverage import CoverageConfiguration
from .driver import DriverInformation
from .history import InsuranceAndDrivingHistory
from .household import HouseholdInformation
from .profile import AutoInsuranceProfile
from .vehicle import VehicleInformation

__all__ = [
    "AutoInsuranceProfile",
    "CoverageConfiguration",
    "DriverInformation",
    "InsuranceAndDrivingHistory",
    "HouseholdInformation",
    "VehicleInformation",
]
