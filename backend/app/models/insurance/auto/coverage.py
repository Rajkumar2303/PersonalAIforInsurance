"""Coverage configuration models (Ontario auto).

Schema only - NO premium calculation or comparison (deferred to later issues).
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import Field

from ..base import SensitiveBaseModel
from ..enums import (
    CoverageSelectionState,
    DiscountType,
    EndorsementType,
    OptionalBenefitType,
    OwnDamageCoverageType,
    PaymentFrequency,
)


class PolicyTiming(SensitiveBaseModel):
    requested_effective_date: Optional[dt.date] = None
    term_months: Optional[int] = Field(default=None, ge=1, le=12)


class ThirdPartyLiability(SensitiveBaseModel):
    """Selected third-party liability limit in CAD."""

    selected_limit: Optional[int] = Field(default=None, ge=0)


class AccidentBenefits(SensitiveBaseModel):
    mandatory_medical_rehab_attendant_care: bool = True
    increased_limits: Optional[bool] = None


class OptionalBenefit(SensitiveBaseModel):
    """One optional accident-benefit item with an explicit state."""

    benefit: OptionalBenefitType
    state: CoverageSelectionState = CoverageSelectionState.UNKNOWN
    detail: Optional[str] = None


class UninsuredAutomobile(SensitiveBaseModel):
    included: bool = True
    limit: Optional[int] = Field(default=None, ge=0)


class DCPD(SensitiveBaseModel):
    """Direct Compensation - Property Damage."""

    included: bool = True
    opted_out: bool = False
    deductible: Optional[float] = Field(default=None, ge=0)


class OwnDamageCoverage(SensitiveBaseModel):
    coverage_type: OwnDamageCoverageType
    deductible: Optional[float] = Field(default=None, ge=0)


class OwnDamage(SensitiveBaseModel):
    items: list[OwnDamageCoverage] = Field(default_factory=list)


class Endorsement(SensitiveBaseModel):
    endorsement: EndorsementType
    state: CoverageSelectionState = CoverageSelectionState.UNKNOWN
    detail: Optional[str] = None


class Discount(SensitiveBaseModel):
    discount: DiscountType
    state: CoverageSelectionState = CoverageSelectionState.UNKNOWN
    detail: Optional[str] = None


class PaymentPreference(SensitiveBaseModel):
    frequency: PaymentFrequency = PaymentFrequency.ANNUAL


class CoverageConfiguration(SensitiveBaseModel):
    timing: PolicyTiming = Field(default_factory=PolicyTiming)
    third_party_liability: ThirdPartyLiability = Field(default_factory=ThirdPartyLiability)
    accident_benefits: AccidentBenefits = Field(default_factory=AccidentBenefits)
    optional_benefits: list[OptionalBenefit] = Field(default_factory=list)
    uninsured_automobile: UninsuredAutomobile = Field(default_factory=UninsuredAutomobile)
    dcpd: DCPD = Field(default_factory=DCPD)
    own_damage: OwnDamage = Field(default_factory=OwnDamage)
    endorsements: list[Endorsement] = Field(default_factory=list)
    discounts: list[Discount] = Field(default_factory=list)
    payment_preference: PaymentPreference = Field(default_factory=PaymentPreference)
