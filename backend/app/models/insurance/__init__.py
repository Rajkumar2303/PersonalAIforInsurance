"""Canonical insurance intake schema models (Issue #2).

Only AUTO is fully implemented. HOME/TENANT/LIFE/TRAVEL/OTHER are recognized
by ``InsuranceType`` but unsupported (product_data stays ``None``; see
``InsuranceProfile.is_supported``).
"""

from __future__ import annotations

from .auto.profile import AutoInsuranceProfile
from .base import SCHEMA_VERSION, SensitiveBaseModel
from .common import (
    AddressInformation,
    ApplicantIdentity,
    ApplicantInformation,
    ConsentState,
    ContactInformation,
)
from .enums import (
    ChannelType,
    CoverageSelectionState,
    DiscountType,
    DriverRole,
    EndorsementType,
    FuelType,
    Gender,
    InsuranceType,
    LicenceClass,
    LicenceStatus,
    MaritalStatus,
    OptionalBenefitType,
    OwnDamageCoverageType,
    OwnershipType,
    PaymentFrequency,
    PreferredLanguage,
    Province,
    PurchaseState,
    QuoteMode,
    RelationshipType,
    VehicleUseType,
)
from .profile import InsuranceProfile

__all__ = [
    "SCHEMA_VERSION",
    "SensitiveBaseModel",
    "InsuranceProfile",
    "AutoInsuranceProfile",
    "ConsentState",
    "ApplicantInformation",
    "ApplicantIdentity",
    "ContactInformation",
    "AddressInformation",
    "InsuranceType",
    "QuoteMode",
    "ChannelType",
    "Province",
    "PreferredLanguage",
    "Gender",
    "MaritalStatus",
    "DriverRole",
    "LicenceStatus",
    "LicenceClass",
    "FuelType",
    "OwnershipType",
    "PurchaseState",
    "VehicleUseType",
    "CoverageSelectionState",
    "PaymentFrequency",
    "OwnDamageCoverageType",
    "OptionalBenefitType",
    "EndorsementType",
    "DiscountType",
    "RelationshipType",
]
