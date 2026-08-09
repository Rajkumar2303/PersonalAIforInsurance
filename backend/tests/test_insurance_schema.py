"""Tests for the canonical insurance intake schema (Issue #2)."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.models.insurance import SCHEMA_VERSION, InsuranceType, QuoteMode
from app.models.insurance.auto.coverage import (
    CoverageConfiguration,
    OptionalBenefit,
    OptionalBenefitType,
    ThirdPartyLiability,
)
from app.models.insurance.auto.driver import DriverInformation, LicenceIdentity
from app.models.insurance.auto.history import ClaimRecord
from app.models.insurance.auto.vehicle import VehicleIdentity, VehicleUse
from app.models.insurance.common import AddressInformation
from tests.factories import SYNTHETIC_VIN, make_insurance_profile


# --- 1/2. valid profiles --------------------------------------------

def test_minimal_auto_profile_valid() -> None:
    profile = make_insurance_profile()
    assert profile.insurance_type is InsuranceType.AUTO
    assert profile.is_supported is True
    assert profile.product_data is not None
    assert len(profile.product_data.drivers) == 1
    assert len(profile.product_data.vehicles) == 1


def test_full_auto_profile_valid() -> None:
    profile = make_insurance_profile()
    auto = profile.product_data
    assert auto is not None
    assert len(auto.history.accidents_and_claims) == 1
    assert auto.coverage.third_party_liability.selected_limit == 2_000_000
    assert any(
        b.benefit is OptionalBenefitType.INCOME_REPLACEMENT for b in auto.coverage.optional_benefits
    )
    assert len(auto.household.licensed_household_members) == 1


# --- 3. invalid insurance_type --------------------------------------

def test_invalid_insurance_type() -> None:
    with pytest.raises(ValidationError):
        make_insurance_profile(insurance_type="bogus")  # type: ignore[arg-type]


# --- 4. unsupported product types ------------------------------------

@pytest.mark.parametrize("product", ["home", "tenant", "life", "travel", "other"])
def test_unsupported_product_type_is_valid_but_unsupported(product: str) -> None:
    profile = make_insurance_profile(insurance_type=product, with_product=False)
    assert profile.insurance_type.value == product
    assert profile.is_supported is False
    assert profile.product_data is None


def test_auto_requires_product_data() -> None:
    with pytest.raises(ValidationError, match="AUTO insurance_type requires product_data"):
        make_insurance_profile(insurance_type=InsuranceType.AUTO, with_product=False)


def test_unsupported_product_cannot_carry_auto_data() -> None:
    with pytest.raises(ValidationError, match="cannot carry product_data"):
        make_insurance_profile(
            insurance_type=InsuranceType.HOME,
            with_product=True,  # would smuggle AutoInsuranceProfile into HOME
        )


# --- 5. postal code ---------------------------------------------------

def test_invalid_postal_code() -> None:
    with pytest.raises(ValidationError, match="postal code"):
        AddressInformation(
            street="1 Main St",
            city="Testville",
            province="ON",
            postal_code="12345",
        )


def test_postal_code_normalized() -> None:
    addr = AddressInformation(
        street="1 Main St",
        city="Testville",
        province="ON",
        postal_code="m0a0a0",
    )
    assert addr.postal_code == "M0A 0A0"


# --- 6/7/8. vehicle year / kilometres / percentages -------------------

def test_invalid_vehicle_year() -> None:
    with pytest.raises(ValidationError, match="model_year"):
        VehicleIdentity(vin=SYNTHETIC_VIN, model_year=1850, make="X", model="Y")


def test_invalid_annual_kilometres() -> None:
    with pytest.raises(ValidationError):
        VehicleUse(annual_kilometres=-5)


def test_invalid_percentage_fields() -> None:
    with pytest.raises(ValidationError):
        VehicleUse(business_use_percentage=150)
    with pytest.raises(ValidationError):
        ClaimRecord(claim_date=dt.date(2021, 1, 1), fault_percentage=101)


# --- 9. nested driver validation --------------------------------------

def test_nested_driver_requires_licence_fields() -> None:
    with pytest.raises(ValidationError):
        DriverInformation(licence=LicenceIdentity(name_on_licence="X"))  # missing required


# --- 10. accident/claim list validation -------------------------------

def test_claims_list_rejects_bad_amount() -> None:
    with pytest.raises(ValidationError):
        ClaimRecord(claim_date=dt.date(2020, 1, 1), paid_or_estimated_amount=-10)


# --- 11. coverage configuration validation ----------------------------

def test_coverage_validation() -> None:
    with pytest.raises(ValidationError):
        CoverageConfiguration(
            third_party_liability=ThirdPartyLiability(selected_limit=-1)
        )
    with pytest.raises(ValidationError):
        CoverageConfiguration(
            optional_benefits=[OptionalBenefit(benefit=OptionalBenefitType.DEATH, state="bogus")]
        )


# --- 12. enum serialization -------------------------------------------

def test_enum_serialization() -> None:
    profile = make_insurance_profile()
    dumped = profile.model_dump(mode="json")
    assert dumped["insurance_type"] == "auto"
    assert dumped["consent"]["quote_mode"] == "live_quote"
    assert dumped["product_data"]["vehicles"][0]["identity"]["fuel_type"] == "gasoline"
    # Enum members carry lowercase values; member names stay uppercase.
    assert InsuranceType.AUTO.value == "auto"
    assert QuoteMode.LIVE_QUOTE.name == "LIVE_QUOTE"


def test_insurance_type_covers_all_planned_products() -> None:
    assert {t.value for t in InsuranceType} == {
        "auto", "home", "tenant", "life", "travel", "other",
    }


# --- 13. schema version -----------------------------------------------

def test_schema_version() -> None:
    assert SCHEMA_VERSION == "1.0"
    assert make_insurance_profile().schema_version == "1.0"


# --- 16. optional fields can remain unset -----------------------------

def test_optional_fields_can_be_unset() -> None:
    profile = make_insurance_profile()
    assert profile.applicant.contact.home_phone is None
    assert profile.applicant.contact.work_phone is None
    auto = profile.product_data
    assert auto is not None
    assert auto.vehicles[0].ownership.purchase_price is None
    assert auto.vehicles[0].use.annual_kilometres is None
    assert auto.history.current_insurance.current_insurer is None


# --- helpers (lightweight missing-field / trace metadata) -------------

def test_missing_fields_helper() -> None:
    # A fully-populated AUTO profile has no missing required-for-live-quote paths.
    complete = make_insurance_profile()
    assert complete.get_missing_fields() == set()

    # Unsupported products carry no product data -> the AUTO product paths are
    # reported missing (shared consent/applicant paths still resolve).
    home = make_insurance_profile(insurance_type="home", with_product=False)
    assert home.get_missing_fields() == {
        "product_data.drivers[0].licence.licence_number",
        "product_data.vehicles[0].identity.vin",
    }


def test_trace_metadata_is_safe() -> None:
    meta = make_insurance_profile().trace_metadata()
    assert set(meta) == {"insurance_type", "schema_version", "is_supported", "missing_field_count"}
    assert meta["insurance_type"] == "auto"
    assert meta["missing_field_count"] == 0
