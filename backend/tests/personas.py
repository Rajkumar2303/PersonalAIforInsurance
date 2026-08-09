"""Reusable synthetic AUTO insurance personas (Issue #5; reusable by #6-#12).

Three personas are composed from the low-level factories in ``factories.py``
using a **base profile + small overrides** architecture:

    make_standard_auto_profile()     # fully/mostly populated, live-quote ready
    make_progressive_auto_profile()  # valid DRAFT missing route-specific fields
    make_edge_case_auto_profile()    # complexity: household driver, claim,
                                     # conviction, insurance interruption

Design rules:

- Built from the real Pydantic ``InsuranceProfile`` models (via factories).
- Individual fields are easy to override in tests - either a friendly keyword
  (``make_standard_auto_profile(annual_kilometres=None)``) or an arbitrary
  canonical field path (``make_standard_auto_profile(**{"applicant.address.city": None})``).
- **Dynamic-field requirement**: the base + overrides architecture means adding
  or removing an optional canonical field never requires rewriting every
  fixture - you add/remove a friendly name in ``FRIENDLY_FIELD_PATHS`` (or just
  pass a canonical-path override) and edit a single base builder.

All values are obviously synthetic and must never correspond to a real person:
reserved phone ranges (555-01xx), all-zero identifiers, the classic fake postal
code M0A 0A0, and clearly-labelled synthetic free text.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.models.insurance import (
    AddressInformation,
    ApplicantIdentity,
    ApplicantInformation,
    ContactInformation,
    InsuranceProfile,
    InsuranceType,
    Province,
    QuoteMode,
)
from app.models.insurance.auto.coverage import (
    AccidentBenefits,
    CoverageConfiguration,
    DCPD,
    Discount,
    DiscountType,
    OptionalBenefit,
    OptionalBenefitType,
    OwnDamage,
    OwnDamageCoverage,
    OwnDamageCoverageType,
    PaymentPreference,
    PolicyTiming,
    ThirdPartyLiability,
    UninsuredAutomobile,
)
from app.models.insurance.auto.driver import OtherDriver
from app.models.insurance.auto.history import (
    CancellationEvent,
    ClaimRecord,
    ConvictionRecord,
    CurrentInsurance,
    InsuranceAndDrivingHistory,
)
from app.models.insurance.auto.household import HouseholdInformation, LicensedHouseholdMember
from factories import (
    make_applicant,
    make_auto_profile,
    make_consent,
    make_driver,
    make_insurance_profile,
    make_vehicle,
)

# --- obviously-synthetic constants --------------------------------------

SYNTHETIC_PRINCIPAL_NAME = "Test Applicant"
SYNTHETIC_HOUSEHOLD_DRIVER_NAME = "Test Household Driver"
SYNTHETIC_DEPENDANT_NAME = "Test Dependant"
SYNTHETIC_INSURER = "Synthetic Mutual"
SYNTHETIC_POLICY_STANDARD = "SYN-0000001"
SYNTHETIC_POLICY_EDGE = "SYN-0000002"
SYNTHETIC_DOB = dt.date(1990, 1, 1)

# --- friendly-name -> canonical path map --------------------------------
# These are the common, frequently-overridden fields. Anything not listed can
# still be overridden by passing a canonical field path directly.

FRIENDLY_FIELD_PATHS: dict[str, str] = {
    "legal_name": "applicant.identity.legal_name",
    "date_of_birth": "applicant.identity.date_of_birth",
    "street": "applicant.address.street",
    "city": "applicant.address.city",
    "postal_code": "applicant.address.postal_code",
    "years_at_current_address": "applicant.address.years_at_current_address",
    "annual_kilometres": "product_data.vehicles[0].use.annual_kilometres",
    "one_way_commute_km": "product_data.vehicles[0].use.one_way_commute_distance_km",
    "tpl_selected_limit": "product_data.coverage.third_party_liability.selected_limit",
    "licence_number": "product_data.drivers[0].licence.licence_number",
    "vin": "product_data.vehicles[0].identity.vin",
}


def _apply_overrides(
    profile: InsuranceProfile,
    friendly: dict[str, Any],
    path_overrides: dict[str, Any],
) -> InsuranceProfile:
    """Apply friendly-name + canonical-path overrides via validated updates.

    Uses the Issue #3/#5 canonical-path mechanism (``InsuranceProfile.updated``)
    so every override is validated by the real schema and no fixture knows field
    internals.
    """
    for name, value in friendly.items():
        if name not in FRIENDLY_FIELD_PATHS:
            raise ValueError(f"unknown friendly override {name!r}")
        profile = profile.updated(FRIENDLY_FIELD_PATHS[name], value)
    for path, value in path_overrides.items():
        profile = profile.updated(path, value)
    return profile


# --- base builders ------------------------------------------------------

def _standard_base() -> InsuranceProfile:
    """A realistic, mostly-complete Ontario auto profile (1 driver, 1 vehicle,
    normal history, standard coverage)."""
    applicant = make_applicant(
        identity=ApplicantIdentity(
            legal_name=SYNTHETIC_PRINCIPAL_NAME,
            preferred_language="english",
            date_of_birth=SYNTHETIC_DOB,
            gender="female",
            marital_status="married",
        ),
        contact=ContactInformation(
            email="test.applicant@example.com",
            mobile_phone="416-555-0199",
            home_phone=None,
            work_phone=None,
            preferred_callback_window="evenings",
        ),
        address=AddressInformation(
            street="123 Test Street",
            unit="Unit 4",
            city="Testville",
            province=Province.ON,
            postal_code="M0A 0A0",
            years_at_current_address=4,
            residence_start_date=dt.date(2022, 3, 1),
            normal_residence_confirmation=True,
            garaging_location_confirmation=True,
        ),
    )
    driver = make_driver(
        licence={
            "name_on_licence": SYNTHETIC_PRINCIPAL_NAME,
            "licence_number": "T0000-0000000-0000",
            "province": "ON",
            "licence_class": "G",
            "status": "valid",
            "expiry_date": dt.date(2030, 12, 31),
        },
        timeline={
            "g2_year": 2009,
            "g_year": 2012,
            "first_licensed_year_canada_us": 2008,
        },
        training={"approved_driver_training_completed": True, "certificate_available": True},
        assignment={"role": "principal", "vehicle_reference": "vehicle_1", "percentage_use": 100.0},
        discount_eligibility={"good_driver_eligible": True},
    )
    vehicle = make_vehicle(
        identity={
            "vin": "1HGCM82633A000000",
            "model_year": 2022,
            "make": "TestMake",
            "model": "TestModel",
            "trim": "LX",
            "body_type": "sedan",
            "fuel_type": "gasoline",
            "cylinders": 4,
        },
        ownership={"ownership_type": "owned", "purchase_state": "used"},
        use={
            "use_types": ["pleasure", "commute"],
            "one_way_commute_distance_km": 18,
            "annual_kilometres": 12000,
            "commuting_days_per_week": 5,
            "carpool": False,
        },
        risk={"winter_tires": True, "unrepaired_damage": False, "modifications": False},
    )
    history = InsuranceAndDrivingHistory(
        current_insurance=CurrentInsurance(
            current_insurer=SYNTHETIC_INSURER,
            policy_number=SYNTHETIC_POLICY_STANDARD,
            expiry_date=dt.date(2027, 6, 30),
            current_premium=1800.0,
            years_continuously_insured=8,
            reason_for_shopping="better rate (synthetic)",
        )
    )
    coverage = CoverageConfiguration(
        timing=PolicyTiming(requested_effective_date=dt.date(2026, 9, 1), term_months=12),
        third_party_liability=ThirdPartyLiability(selected_limit=2_000_000),
        accident_benefits=AccidentBenefits(mandatory_medical_rehab_attendant_care=True, increased_limits=True),
        optional_benefits=[
            OptionalBenefit(benefit=OptionalBenefitType.INCOME_REPLACEMENT, state="included"),
            OptionalBenefit(benefit=OptionalBenefitType.CAREGIVER, state="excluded"),
        ],
        uninsured_automobile=UninsuredAutomobile(included=True, limit=2_000_000),
        dcpd=DCPD(included=True, opted_out=False, deductible=500.0),
        own_damage=OwnDamage(items=[OwnDamageCoverage(coverage_type="comprehensive", deductible=500.0)]),
        discounts=[
            Discount(discount=DiscountType.BUNDLE, state="included"),
            Discount(discount=DiscountType.WINTER_TIRES, state="included"),
            Discount(discount=DiscountType.CLAIMS_FREE, state="included"),
            Discount(discount=DiscountType.CONVICTION_FREE, state="included"),
        ],
        payment_preference=PaymentPreference(frequency="annual"),
    )
    return make_insurance_profile(
        consent=make_consent(),
        applicant=applicant,
        product_data=make_auto_profile(
            drivers=[driver],
            vehicles=[vehicle],
            household=HouseholdInformation(
                licensed_household_members=[
                    LicensedHouseholdMember(name="Test Co-Applicant", relationship="spouse", has_own_policy=False)
                ],
                total_household_vehicles=1,
            ),
            history=history,
            coverage=coverage,
        ),
    )


def _edge_case_base() -> InsuranceProfile:
    """Standard scaffold + legitimate complexity (all synthetic)."""
    base = _standard_base()
    assert base.product_data is not None
    auto = base.product_data

    principal = auto.drivers[0].model_copy(
        update={
            "other_drivers": [
                OtherDriver(
                    name=SYNTHETIC_HOUSEHOLD_DRIVER_NAME,
                    has_own_policy=False,
                    exclusion_may_be_required=False,
                )
            ]
        }
    )
    history = InsuranceAndDrivingHistory(
        current_insurance=CurrentInsurance(
            current_insurer=SYNTHETIC_INSURER,
            policy_number=SYNTHETIC_POLICY_EDGE,
            expiry_date=dt.date(2027, 3, 31),
            current_premium=2400.0,
            years_continuously_insured=0.0,
            reason_for_shopping="prior cancellation (synthetic)",
        ),
        cancellations=[
            CancellationEvent(cancellation_date=dt.date(2024, 1, 15), reason="non-payment (synthetic)")
        ],
        accidents_and_claims=[
            ClaimRecord(
                driver_reference="driver_1",
                vehicle_reference="vehicle_1",
                claim_date=dt.date(2023, 9, 2),
                fault_percentage=25.0,
                coverage="collision",
                paid_or_estimated_amount=2400.0,
                details="synthetic minor collision at low speed",
            )
        ],
        convictions=[
            ConvictionRecord(
                driver_reference="driver_1",
                conviction_date=dt.date(2022, 4, 10),
                description="synthetic speeding ticket",
            )
        ],
    )
    household = HouseholdInformation(
        licensed_household_members=[
            LicensedHouseholdMember(
                name=SYNTHETIC_HOUSEHOLD_DRIVER_NAME,
                relationship="spouse",
                has_own_policy=False,
                exclusion_may_be_required=False,
            ),
            LicensedHouseholdMember(name=SYNTHETIC_DEPENDANT_NAME, relationship="child", has_own_policy=False),
        ],
        total_household_vehicles=1,
    )
    auto = auto.model_copy(update={"drivers": [principal], "household": household, "history": history})
    return base.model_copy(update={"product_data": auto})


# --- personas -----------------------------------------------------------

def make_standard_auto_profile(
    *,
    legal_name: str = SYNTHETIC_PRINCIPAL_NAME,
    date_of_birth: Optional[dt.date] = SYNTHETIC_DOB,
    years_at_current_address: Optional[int] = 4,
    annual_kilometres: Optional[float] = 12000,
    one_way_commute_km: Optional[float] = 18,
    tpl_selected_limit: Optional[int] = 2_000_000,
    **path_overrides: Any,
) -> InsuranceProfile:
    """STANDARD_COMPLETE_PROFILE: one driver, one vehicle, normal history,
    standard coverage. Missing nothing - ``is_live_quote_ready`` is True."""
    profile = _standard_base()
    return _apply_overrides(
        profile,
        {
            "legal_name": legal_name,
            "date_of_birth": date_of_birth,
            "years_at_current_address": years_at_current_address,
            "annual_kilometres": annual_kilometres,
            "one_way_commute_km": one_way_commute_km,
            "tpl_selected_limit": tpl_selected_limit,
        },
        path_overrides,
    )


def make_progressive_auto_profile(
    *,
    legal_name: str = SYNTHETIC_PRINCIPAL_NAME,
    date_of_birth: Optional[dt.date] = None,
    years_at_current_address: Optional[int] = None,
    annual_kilometres: Optional[float] = None,
    one_way_commute_km: Optional[float] = None,
    tpl_selected_limit: Optional[int] = None,
    **path_overrides: Any,
) -> InsuranceProfile:
    """PROGRESSIVE_INCOMPLETE_PROFILE: a valid DRAFT intentionally missing
    route-specific / optional fields (annual kilometres, years at current
    address, selected coverage value, one-way commute, date of birth). Ideal
    for progressive intake, browser-discovered, and voice-discovered fields."""
    profile = _standard_base()
    return _apply_overrides(
        profile,
        {
            "legal_name": legal_name,
            "date_of_birth": date_of_birth,
            "years_at_current_address": years_at_current_address,
            "annual_kilometres": annual_kilometres,
            "one_way_commute_km": one_way_commute_km,
            "tpl_selected_limit": tpl_selected_limit,
        },
        path_overrides,
    )


def make_edge_case_auto_profile(
    *,
    legal_name: str = SYNTHETIC_PRINCIPAL_NAME,
    date_of_birth: Optional[dt.date] = SYNTHETIC_DOB,
    years_at_current_address: Optional[int] = 3,
    annual_kilometres: Optional[float] = 15000,
    one_way_commute_km: Optional[float] = 22,
    tpl_selected_limit: Optional[int] = 1_000_000,
    **path_overrides: Any,
) -> InsuranceProfile:
    """EDGE_CASE_PROFILE: an applicant with legitimate complexity - an
    additional household driver, a prior claim, a conviction, and an insurance
    interruption. Everything remains clearly synthetic."""
    profile = _edge_case_base()
    return _apply_overrides(
        profile,
        {
            "legal_name": legal_name,
            "date_of_birth": date_of_birth,
            "years_at_current_address": years_at_current_address,
            "annual_kilometres": annual_kilometres,
            "one_way_commute_km": one_way_commute_km,
            "tpl_selected_limit": tpl_selected_limit,
        },
        path_overrides,
    )
