"""Tests for sensitive-data handling in the insurance schema (Issue #2).

Asserts that licence numbers, DOB, full address, VIN, phone, email, policy
numbers, and claims details never appear in safe/redacted output or in
``repr``/``str``.
"""

from __future__ import annotations

import json

from tests.factories import (
    SYNTHETIC_LICENCE,
    SYNTHETIC_VIN,
    make_insurance_profile,
)

SENSITIVE_VALUES = {
    SYNTHETIC_LICENCE,          # licence number
    SYNTHETIC_VIN,              # VIN
    "test.applicant@example.com",  # email
    "416-555-0199",             # phone
    "1990-01-01",               # DOB
    "M0A 0A0",                  # postal code
    "123 Test Street",          # street
    "Testville",                # city
    "parking-lot scrape (synthetic)",  # claim details
}


def _safe_output(profile) -> str:
    return json.dumps(profile.redacted_dict(), sort_keys=True)


def test_redacted_dict_redacts_sensitive_fields() -> None:
    profile = make_insurance_profile()
    redacted = profile.redacted_dict()

    product = redacted["product_data"]
    assert redacted["applicant"]["identity"]["date_of_birth"] == "[REDACTED]"
    assert redacted["applicant"]["identity"]["legal_name"] == "[REDACTED]"
    assert redacted["applicant"]["contact"]["email"] == "[REDACTED]"
    assert redacted["applicant"]["contact"]["mobile_phone"] == "[REDACTED]"
    # Address fields and licence identity are redacted per-field (containers
    # like ``address``/``licence`` are recursed into, not block-redacted).
    assert redacted["applicant"]["address"]["street"] == "[REDACTED]"
    assert redacted["applicant"]["address"]["postal_code"] == "[REDACTED]"
    assert product["drivers"][0]["licence"]["licence_number"] == "[REDACTED]"
    assert product["vehicles"][0]["identity"]["vin"] == "[REDACTED]"
    # Claims free-text details are redacted.
    assert product["history"]["accidents_and_claims"][0]["details"] == "[REDACTED]"


def test_safe_dict_is_same_as_redacted_dict() -> None:
    profile = make_insurance_profile()
    assert profile.safe_dict() == profile.redacted_dict()


def test_no_sensitive_values_in_safe_output() -> None:
    output = _safe_output(make_insurance_profile())
    for value in SENSITIVE_VALUES:
        assert value not in output, f"sensitive value leaked in safe output: {value}"


def test_repr_and_str_do_not_leak() -> None:
    profile = make_insurance_profile()
    for value in SENSITIVE_VALUES:
        assert value not in str(profile)
        assert value not in repr(profile)
    assert "InsuranceProfile(" in repr(profile)


def test_boolean_consent_flags_are_not_redacted() -> None:
    """Booleans (consent flags) cannot contain PII and stay visible."""
    redacted = make_insurance_profile().redacted_dict()
    assert redacted["consent"]["recording_permission"] is False
    assert redacted["consent"]["transcription_permission"] is False
    assert redacted["consent"]["callback_permission"] is True


def test_non_sensitive_fields_preserved() -> None:
    redacted = make_insurance_profile().redacted_dict()
    product = redacted["product_data"]
    assert product["vehicles"][0]["identity"]["make"] == "TestMake"
    assert product["vehicles"][0]["identity"]["model"] == "TestModel"
    assert product["vehicles"][0]["identity"]["model_year"] == 2022
    assert product["coverage"]["third_party_liability"]["selected_limit"] == 2_000_000
    assert redacted["consent"]["quote_mode"] == "live_quote"


def test_redacted_output_keeps_structure() -> None:
    redacted = make_insurance_profile().redacted_dict()
    # Same top-level keys as the raw dump (structure preserved).
    raw = make_insurance_profile().model_dump(mode="json")
    assert set(redacted.keys()) == set(raw.keys())
    assert set(redacted["product_data"].keys()) == set(raw["product_data"].keys())


def test_raw_model_dump_still_contains_data_for_explicit_callers() -> None:
    """model_dump() is explicit and unredacted - only safe paths must be used
    for logging/tracing. This guards the contract documented on the base model."""
    raw = make_insurance_profile().model_dump(mode="json")
    assert raw["applicant"]["identity"]["legal_name"] == "Test Applicant"
