"""Tests for Issue #3 Part A: progressive-profile hardening.

Covers draft profiles, canonical field paths, and the validated dynamic update
primitive that Issue #5/#7/#9 will use.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pytest
from pydantic import ValidationError

from app.models.insurance.auto.profile import AutoInsuranceProfile
from app.models.insurance.common import AddressInformation, ApplicantIdentity
from app.models.insurance.paths import FieldPathError, is_missing, parse_field_path, resolve
from app.models.insurance.profile import ProfileUpdateError
from tests.factories import make_draft_profile, make_insurance_profile


# --- 1/2/3. draft profiles -------------------------------------------

def test_draft_auto_profile_constructible() -> None:
    draft = make_draft_profile()
    assert draft.insurance_type.value == "auto"
    assert draft.is_draft is True
    assert draft.is_live_quote_ready is False


def test_zero_drivers_allowed_in_draft() -> None:
    auto = AutoInsuranceProfile(vehicles=[])
    assert auto.drivers == []
    assert auto.vehicles == []


def test_zero_vehicles_allowed_in_draft() -> None:
    auto = AutoInsuranceProfile(drivers=[])
    assert auto.drivers == []
    assert auto.vehicles == []


# --- 4/5. optional fields ---------------------------------------------

def test_dob_optional_in_draft() -> None:
    identity = ApplicantIdentity(legal_name="Test Applicant")
    assert identity.date_of_birth is None
    # Validation still applies when supplied.
    assert ApplicantIdentity(legal_name="X", date_of_birth=dt.date(1990, 1, 1)).date_of_birth == dt.date(1990, 1, 1)


def test_partial_address_optional_in_draft() -> None:
    address = AddressInformation(province="ON", postal_code="M0A 0A0")
    assert address.street is None
    assert address.city is None
    assert address.postal_code == "M0A 0A0"


# --- 6. live-quote readiness still works ------------------------------

def test_required_for_live_quote_identifies_missing() -> None:
    draft = make_draft_profile()
    missing = draft.get_missing_fields()
    assert "applicant.identity.date_of_birth" in missing
    assert "applicant.address.street" in missing
    assert "applicant.address.city" in missing
    assert "product_data.drivers[0].licence.licence_number" in missing
    # VIN is NOT globally required for a live quote - routes that need a VIN
    # declare it per-route (registry/route data), never via the base schema.
    assert "product_data.vehicles[0].identity.vin" not in missing
    # A complete profile is live-quote ready.
    complete = make_insurance_profile()
    assert complete.is_live_quote_ready is True
    assert complete.get_missing_fields() == set()


# --- 7/8/9. canonical path resolver -----------------------------------

def test_path_parser_and_formatter() -> None:
    assert parse_field_path("product_data.drivers[0].licence.licence_number") == (
        "product_data", "drivers", 0, "licence", "licence_number",
    )
    with pytest.raises(FieldPathError):
        parse_field_path("product_data.drivers[0]junk.licence")


def test_resolver_reads_nested_fields() -> None:
    profile = make_insurance_profile()
    assert resolve(profile, "applicant.identity.legal_name") == "Test Applicant"
    assert resolve(profile, "product_data.coverage.third_party_liability.selected_limit") == 2_000_000


def test_resolver_handles_list_indexes() -> None:
    profile = make_insurance_profile()
    assert resolve(profile, "product_data.vehicles[0].identity.vin") == "1HGCM82633A000000"
    assert is_missing(profile, "product_data.vehicles[0].use.annual_kilometres") is True


def test_resolver_rejects_unknown_paths_safely() -> None:
    profile = make_insurance_profile()
    with pytest.raises(FieldPathError):
        resolve(profile, "applicant.identity.not_a_field")
    with pytest.raises(FieldPathError):
        resolve(profile, "product_data.vehicles[5].identity.vin")  # out of range
    assert is_missing(profile, "applicant.identity.not_a_field") is True


# --- 10/11/12/13/14/15. validated updates ------------------------------

def test_validated_update_annual_kilometres() -> None:
    profile = make_insurance_profile()
    updated = profile.updated("product_data.vehicles[0].use.annual_kilometres", 15_000)
    assert updated.product_data.vehicles[0].use.annual_kilometres == 15_000
    assert profile.product_data.vehicles[0].use.annual_kilometres is None  # immutable


def test_validated_update_liability_limit() -> None:
    profile = make_insurance_profile()
    updated = profile.updated("product_data.coverage.third_party_liability.selected_limit", 5_000_000)
    assert updated.product_data.coverage.third_party_liability.selected_limit == 5_000_000


def test_validated_update_sensitive_field_no_leak() -> None:
    profile = make_insurance_profile()
    value = "T1111-11111-11111"
    updated = profile.updated("product_data.drivers[0].licence.licence_number", value)
    assert updated.product_data.drivers[0].licence.licence_number == value
    # Error messages never contain the value; safe output redacts it.
    assert "T1111" not in str(updated.safe_dict())


def test_invalid_update_fails_validation() -> None:
    profile = make_insurance_profile()
    with pytest.raises(ProfileUpdateError):
        profile.updated("product_data.vehicles[0].use.annual_kilometres", -5)
    with pytest.raises(ProfileUpdateError):
        profile.updated("product_data.coverage.third_party_liability.selected_limit", -1)


def test_unknown_field_update_fails() -> None:
    profile = make_insurance_profile()
    with pytest.raises(FieldPathError):
        profile.updated("product_data.bogus_field", 1)
    with pytest.raises(FieldPathError):
        profile.updated("product_data.vehicles[9].identity.vin", "X" * 17)


def test_update_preserves_unrelated_fields() -> None:
    profile = make_insurance_profile()
    original = profile.model_dump(mode="json")
    updated = profile.updated("product_data.vehicles[0].use.annual_kilometres", 12_000)
    dumped = updated.model_dump(mode="json")
    assert dumped["applicant"] == original["applicant"]
    assert dumped["consent"] == original["consent"]
    assert dumped["product_data"]["vehicles"][0]["identity"] == original["product_data"]["vehicles"][0]["identity"]
    assert dumped["product_data"]["coverage"] == original["product_data"]["coverage"]


# --- 16. model_copy is not the trusted update path ---------------------

def test_model_copy_not_trusted_for_updates() -> None:
    """model_copy(update=...) does NOT revalidate in Pydantic v2, so the
    project's trusted dynamic-update path is the validated ``updated()``."""
    profile = make_insurance_profile()
    # Raw model_copy silently accepts an invalid profile (AUTO without product_data).
    invalid = profile.model_copy(update={"product_data": None})
    assert invalid.product_data is None and invalid.is_supported is True
    # The trusted path rejects exactly that invariant break.
    with pytest.raises(ProfileUpdateError):
        profile.updated("product_data", None)
    # And an invalid VALUE via the trusted path raises.
    with pytest.raises(ProfileUpdateError):
        profile.updated("product_data.vehicles[0].identity.model_year", 1850)


# --- 17. dynamic-field maintainability (localized changes) -------------

def test_new_optional_field_requires_no_pipeline_change() -> None:
    """A new optional field on a model is handled by the generic resolver /
    missing-checker with zero special-casing."""

    class AddressWithYears(AddressInformation):
        years_at_current_address: Optional[int] = None

    address = AddressWithYears(street="1 Test St", city="Testville", province="ON", postal_code="M0A 0A0", years_at_current_address=3)
    assert resolve(address, "years_at_current_address") == 3
    assert resolve(address, "postal_code") == "M0A 0A0"
    assert is_missing(address, "years_at_current_address") is False
    assert is_missing(AddressWithYears(street="1 Test St", city="T", province="ON", postal_code="M0A 0A0"), "years_at_current_address") is True


def test_set_field_alias() -> None:
    profile = make_insurance_profile()
    updated = profile.set_field("applicant.identity.date_of_birth", dt.date(1985, 5, 5))
    assert updated.applicant.identity.date_of_birth == dt.date(1985, 5, 5)
