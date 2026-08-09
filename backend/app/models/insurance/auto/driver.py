"""Driver information models (Ontario auto)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import Field

from ..base import SensitiveBaseModel
from ..enums import DriverRole, LicenceClass, LicenceStatus, Province


class LicenceIdentity(SensitiveBaseModel):
    """Driver's licence identity. SENSITIVE: licence_number (redacted)."""

    name_on_licence: str
    licence_number: str
    province: Province = Province.ON
    licence_class: LicenceClass = LicenceClass.G
    status: LicenceStatus = LicenceStatus.VALID
    expiry_date: dt.date


class LicensingTimeline(SensitiveBaseModel):
    """Ontario graduated-licence timeline (years are sufficient granularity)."""

    g1_year: Optional[int] = Field(default=None, ge=1950, le=2100)
    g2_year: Optional[int] = Field(default=None, ge=1950, le=2100)
    g_year: Optional[int] = Field(default=None, ge=1950, le=2100)
    first_licensed_year_canada_us: Optional[int] = Field(default=None, ge=1950, le=2100)
    recognized_out_of_country_experience: Optional[bool] = None
    proof_available: Optional[bool] = None


class DriverTraining(SensitiveBaseModel):
    approved_driver_training_completed: bool = False
    certificate_available: Optional[bool] = None


class DriverAssignment(SensitiveBaseModel):
    """How the driver uses vehicles. References vehicles by stable ``label``."""

    role: DriverRole = DriverRole.PRINCIPAL
    vehicle_reference: Optional[str] = None
    percentage_use: Optional[float] = Field(default=None, ge=0, le=100)
    other_regular_access: Optional[bool] = None


class DiscountEligibility(SensitiveBaseModel):
    retiree_eligible: Optional[bool] = None
    student_status: Optional[bool] = None
    good_driver_eligible: Optional[bool] = None
    group_affinity_eligible: Optional[bool] = None
    telematics_opt_in: Optional[bool] = None


class OtherDriver(SensitiveBaseModel):
    """Another person with regular access to the insured vehicles."""

    name: str
    licence_class: Optional[LicenceClass] = None
    has_own_policy: Optional[bool] = None
    exclusion_may_be_required: Optional[bool] = None


class DriverInformation(SensitiveBaseModel):
    """A single driver on the auto policy. SENSITIVE: licence identity."""

    licence: LicenceIdentity
    timeline: LicensingTimeline = Field(default_factory=LicensingTimeline)
    training: DriverTraining = Field(default_factory=DriverTraining)
    assignment: DriverAssignment = Field(default_factory=DriverAssignment)
    discount_eligibility: DiscountEligibility = Field(default_factory=DiscountEligibility)
    other_drivers: list[OtherDriver] = Field(default_factory=list)
