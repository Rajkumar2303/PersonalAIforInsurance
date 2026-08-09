"""Household and fleet models (Ontario auto)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import Field

from ..base import SensitiveBaseModel
from ..enums import DriverRole, LicenceClass, RelationshipType


class LicensedHouseholdMember(SensitiveBaseModel):
    name: str
    relationship: Optional[RelationshipType] = None
    licence_class: Optional[LicenceClass] = None
    has_own_policy: Optional[bool] = None
    exclusion_may_be_required: Optional[bool] = None


class RegularVehicleUser(SensitiveBaseModel):
    name: str
    relationship: Optional[RelationshipType] = None


class Dependant(SensitiveBaseModel):
    name: str
    date_of_birth: Optional[dt.date] = None


class OtherHouseholdVehicle(SensitiveBaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    model_year: Optional[int] = Field(default=None, ge=1900)
    owned_by: Optional[str] = None


class DriverVehicleAssignment(SensitiveBaseModel):
    """Maps a driver (by reference) to a vehicle (by reference) with usage."""

    driver_reference: str
    vehicle_reference: str
    role: DriverRole = DriverRole.SECONDARY
    percentage_use: Optional[float] = Field(default=None, ge=0, le=100)


class HouseholdInformation(SensitiveBaseModel):
    licensed_household_members: list[LicensedHouseholdMember] = Field(default_factory=list)
    regular_vehicle_users: list[RegularVehicleUser] = Field(default_factory=list)
    dependants: list[Dependant] = Field(default_factory=list)
    other_household_vehicles: list[OtherHouseholdVehicle] = Field(default_factory=list)
    total_household_vehicles: Optional[int] = Field(default=None, ge=0)
    driver_to_vehicle_assignments: list[DriverVehicleAssignment] = Field(default_factory=list)
