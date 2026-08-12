"""Synthetic test-data factories for the insurance schema.

All data is obviously fake - never real personal information. Uses reserved /
clearly-synthetic values (555-01xx phone range, all-zero identifiers, the
classic fake Canadian postal code M0A 0A0) so no fixture could be mistaken for
a real credential.
"""

from __future__ import annotations

import datetime as dt

from app.models.insurance import (
    AddressInformation,
    ApplicantIdentity,
    ApplicantInformation,
    ChannelType,
    ConsentState,
    ContactInformation,
    InsuranceProfile,
    InsuranceType,
    Province,
    QuoteMode,
)
from app.models.insurance.auto.coverage import (
    CoverageConfiguration,
    Discount,
    DiscountType,
    OptionalBenefit,
    OptionalBenefitType,
    ThirdPartyLiability,
)
from app.models.insurance.auto.driver import DriverInformation, LicenceIdentity
from app.models.insurance.auto.history import ClaimRecord, InsuranceAndDrivingHistory
from app.models.insurance.auto.household import HouseholdInformation, LicensedHouseholdMember
from app.models.insurance.auto.profile import AutoInsuranceProfile
from app.models.insurance.auto.vehicle import VehicleIdentity, VehicleInformation

SYNTHETIC_LICENCE = "T0000-00000-00000"
SYNTHETIC_VIN = "1HGCM82633A000000"
SYNTHETIC_POSTAL = "M0A 0A0"


def make_consent(**overrides) -> ConsentState:
    base = dict(
        consent_timestamp=dt.datetime(2026, 1, 1, 9, 0),
        quote_mode=QuoteMode.LIVE_QUOTE,
        permitted_channels=[ChannelType.EMAIL, ChannelType.PHONE],
        approved_insurers_or_brokers=None,
        callback_permission=True,
        recording_permission=False,
        transcription_permission=False,
    )
    base.update(overrides)
    return ConsentState(**base)


def make_applicant(**overrides) -> ApplicantInformation:
    base = dict(
        identity=ApplicantIdentity(
            legal_name="Test Applicant",
            alias=None,
            preferred_language="english",
            date_of_birth=dt.date(1990, 1, 1),
            gender="female",
            marital_status="single",
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
            postal_code=SYNTHETIC_POSTAL,
            residence_start_date=dt.date(2020, 6, 1),
            prior_address=None,
            normal_residence_confirmation=True,
            garaging_location_confirmation=True,
        ),
    )
    base.update(overrides)
    return ApplicantInformation(**base)


def make_driver(**overrides) -> DriverInformation:
    base = dict(
        licence=LicenceIdentity(
            name_on_licence="Test Applicant",
            licence_number=SYNTHETIC_LICENCE,
            province=Province.ON,
            licence_class="G",
            status="valid",
            expiry_date=dt.date(2030, 12, 31),
        ),
        timeline={},
        training={},
        assignment={},
        discount_eligibility={},
        other_drivers=[],
    )
    base.update(overrides)
    return DriverInformation(**base)


def make_vehicle(**overrides) -> VehicleInformation:
    base = dict(
        label="vehicle_1",
        identity=VehicleIdentity(
            vin=SYNTHETIC_VIN,
            model_year=2022,
            make="TestMake",
            model="TestModel",
            trim="LX",
            body_type="sedan",
            fuel_type="gasoline",
            cylinders=4,
        ),
        ownership={},
        use={},
        risk={},
        special_use={},
    )
    base.update(overrides)
    return VehicleInformation(**base)


def make_auto_profile(**overrides) -> AutoInsuranceProfile:
    base = dict(
        drivers=[make_driver()],
        vehicles=[make_vehicle()],
        household=HouseholdInformation(
            licensed_household_members=[
                LicensedHouseholdMember(name="Test Co-Applicant", relationship="spouse")
            ],
            regular_vehicle_users=[],
            dependants=[],
            other_household_vehicles=[],
            total_household_vehicles=1,
            driver_to_vehicle_assignments=[],
        ),
        history=InsuranceAndDrivingHistory(),
        coverage=CoverageConfiguration(),
    )
    base.update(overrides)
    return AutoInsuranceProfile(**base)


def make_full_auto_profile() -> AutoInsuranceProfile:
    """A richer profile exercising claims, coverage, and household data."""
    history = InsuranceAndDrivingHistory(
        current_insurance={},
        accidents_and_claims=[
            ClaimRecord(
                driver_reference="driver_1",
                vehicle_reference="vehicle_1",
                claim_date=dt.date(2021, 5, 10),
                fault_percentage=0,
                coverage="collision",
                paid_or_estimated_amount=1500.0,
                details="parking-lot scrape (synthetic)",
            )
        ],
        convictions=[],
    )
    coverage = CoverageConfiguration(
        third_party_liability=ThirdPartyLiability(selected_limit=2_000_000),
        optional_benefits=[
            OptionalBenefit(benefit=OptionalBenefitType.INCOME_REPLACEMENT, state="included"),
            OptionalBenefit(benefit=OptionalBenefitType.CAREGIVER, state="excluded"),
        ],
        discounts=[
            Discount(discount=DiscountType.BUNDLE, state="included"),
            Discount(discount=DiscountType.WINTER_TIRES, state="included"),
        ],
    )
    return make_auto_profile(history=history, coverage=coverage)


def make_insurance_profile(
    insurance_type: InsuranceType = InsuranceType.AUTO,
    *,
    with_product: bool = True,
    **overrides,
) -> InsuranceProfile:
    base = dict(
        schema_version="1.1",
        insurance_type=insurance_type,
        consent=make_consent(),
        applicant=make_applicant(),
        product_data=make_full_auto_profile() if with_product else None,
    )
    base.update(overrides)
    return InsuranceProfile(**base)


def make_draft_profile() -> InsuranceProfile:
    """A minimal AUTO DRAFT profile: consent + basic applicant only.

    No date_of_birth, no street/city, zero drivers and zero vehicles - the
    schema allows this after Issue #3 hardening. Live-quote completeness is
    reported separately via ``required_for_live_quote()``/``get_missing_fields()``.
    """
    return InsuranceProfile(
        insurance_type=InsuranceType.AUTO,
        consent=make_consent(),
        applicant=ApplicantInformation(
            identity=ApplicantIdentity(legal_name="Test Applicant"),
            contact=ContactInformation(),
            address=AddressInformation(province=Province.ON, postal_code=SYNTHETIC_POSTAL),
        ),
        product_data=AutoInsuranceProfile(),
    )
