"""Vehicle information models (Ontario auto)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import Field, field_validator

from ..base import SensitiveBaseModel
from ..common import AddressInformation
from ..enums import FuelType, OwnershipType, PurchaseState, VehicleUseType

_VIN_RE = r"^[A-HJ-NPR-Z0-9]{17}$"


class VehicleIdentity(SensitiveBaseModel):
    """Vehicle identity. SENSITIVE: VIN (redacted)."""

    vin: str
    model_year: int
    make: str
    model: str
    trim: Optional[str] = None
    body_type: Optional[str] = None
    fuel_type: FuelType = FuelType.GASOLINE
    cylinders: Optional[int] = Field(default=None, ge=1, le=16)
    engine_size: Optional[str] = None
    gvwr: Optional[int] = Field(default=None, ge=0)

    @field_validator("vin")
    @classmethod
    def _validate_vin(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 17:
            raise ValueError("VIN must be exactly 17 characters")
        return normalized

    @field_validator("model_year")
    @classmethod
    def _validate_model_year(cls, value: int) -> int:
        current = dt.date.today().year
        if value < 1900 or value > current + 1:
            raise ValueError(f"model_year must be between 1900 and {current + 1}")
        return value


class VehicleOwnership(SensitiveBaseModel):
    ownership_type: OwnershipType = OwnershipType.OWNED
    purchase_state: Optional[PurchaseState] = None
    purchase_or_lease_date: Optional[dt.date] = None
    purchase_price: Optional[float] = Field(default=None, ge=0)
    registered_owner: Optional[str] = None
    actual_owner: Optional[str] = None
    lienholder: Optional[str] = None
    lessor: Optional[str] = None


class VehicleUse(SensitiveBaseModel):
    use_types: list[VehicleUseType] = Field(
        default_factory=lambda: [VehicleUseType.PLEASURE]
    )
    one_way_commute_distance_km: Optional[float] = Field(default=None, ge=0)
    annual_kilometres: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    business_use_percentage: Optional[float] = Field(default=None, ge=0, le=100)
    commuting_days_per_week: Optional[int] = Field(default=None, ge=0, le=7)
    carpool: Optional[bool] = None
    passenger_count: Optional[int] = Field(default=None, ge=0)


class VehicleRiskDetails(SensitiveBaseModel):
    garaging_address: Optional[AddressInformation] = None
    unrepaired_damage: Optional[bool] = None
    modifications: Optional[bool] = None
    non_factory_equipment: Optional[bool] = None
    winter_tires: Optional[bool] = None
    theft_recovery_device: Optional[bool] = None
    anti_theft_features: list[str] = Field(default_factory=list)


class VehicleSpecialUse(SensitiveBaseModel):
    """Explicit specialty/commercial flags (may trigger specialty routing later)."""

    rideshare: bool = False
    delivery: bool = False
    carshare: bool = False
    rental_to_others: bool = False
    passengers_for_compensation: bool = False
    trailer_use: Optional[bool] = None
    hazardous_material_use: Optional[bool] = None


class VehicleInformation(SensitiveBaseModel):
    """A single insured vehicle, with a stable ``label`` for assignments."""

    label: str = "vehicle_1"
    identity: VehicleIdentity
    ownership: VehicleOwnership = Field(default_factory=VehicleOwnership)
    use: VehicleUse = Field(default_factory=VehicleUse)
    risk: VehicleRiskDetails = Field(default_factory=VehicleRiskDetails)
    special_use: VehicleSpecialUse = Field(default_factory=VehicleSpecialUse)
