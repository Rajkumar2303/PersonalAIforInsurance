"""Tests for the reusable synthetic AUTO personas (Issue #5).

Confirms each persona validates, the progressive persona is a draft, fixtures
are overridable without touching workflow code, and sensitive synthetic values
stay out of safe/redacted outputs (so they can never leak into logs/traces).
"""

from __future__ import annotations

import json

import pytest

from app.models.insurance.enums import InsuranceType
from app.models.insurance.paths import is_missing
from app.services.intake.vault import InMemoryProfileVault

from personas import (
    FRIENDLY_FIELD_PATHS,
    SYNTHETIC_HOUSEHOLD_DRIVER_NAME,
    make_edge_case_auto_profile,
    make_progressive_auto_profile,
    make_standard_auto_profile,
)

SENSITIVE_MARKERS = [
    "T0000-0000000-0000",  # licence number
    "1HGCM82633A000000",  # VIN
    "M0A 0A0",  # postal code
    "416-555-0199",  # phone
    "test.applicant@example.com",  # email
    "1990-01-01",  # DOB
    "123 Test Street",  # street
    "SYN-0000001",  # standard policy number
    "SYN-0000002",  # edge policy number
    "synthetic minor collision at low speed",  # claim details
    "synthetic speeding ticket",  # conviction description
]


# --- personas validate --------------------------------------------------

def test_standard_profile_validates_and_is_live_quote_ready() -> None:
    profile = make_standard_auto_profile()
    assert profile.insurance_type is InsuranceType.AUTO
    assert profile.is_supported is True
    assert profile.is_draft is False
    assert profile.is_live_quote_ready is True
    assert profile.get_missing_fields() == set()


def test_standard_profile_is_populated() -> None:
    profile = make_standard_auto_profile()
    auto = profile.product_data
    assert auto is not None
    assert len(auto.drivers) == 1
    assert len(auto.vehicles) == 1
    assert auto.vehicles[0].use.annual_kilometres == 12000
    assert profile.applicant.address.years_at_current_address == 4
    assert auto.coverage.third_party_liability.selected_limit == 2_000_000


def test_progressive_profile_validates_as_draft() -> None:
    profile = make_progressive_auto_profile()
    assert profile.is_supported is True
    assert profile.is_draft is True
    assert profile.is_live_quote_ready is False


def test_progressive_profile_missing_expected_fields() -> None:
    profile = make_progressive_auto_profile()
    missing = [
        "product_data.vehicles[0].use.annual_kilometres",
        "applicant.address.years_at_current_address",
        "product_data.coverage.third_party_liability.selected_limit",
        "product_data.vehicles[0].use.one_way_commute_distance_km",
        "applicant.identity.date_of_birth",
    ]
    for path in missing:
        assert is_missing(profile, path), f"expected {path} to be missing"


def test_progressive_profile_keeps_core_data() -> None:
    profile = make_progressive_auto_profile()
    auto = profile.product_data
    assert auto is not None
    assert len(auto.drivers) == 1
    assert len(auto.vehicles) == 1
    assert profile.applicant.identity.legal_name == "Test Applicant"
    assert profile.applicant.address.postal_code == "M0A 0A0"


def test_edge_case_profile_validates() -> None:
    profile = make_edge_case_auto_profile()
    assert profile.is_supported is True
    assert profile.is_live_quote_ready is True


def test_edge_case_profile_has_complexity() -> None:
    profile = make_edge_case_auto_profile()
    auto = profile.product_data
    assert auto is not None
    # additional household driver
    assert auto.drivers[0].other_drivers[0].name == SYNTHETIC_HOUSEHOLD_DRIVER_NAME
    household_names = {m.name for m in auto.household.licensed_household_members}
    assert SYNTHETIC_HOUSEHOLD_DRIVER_NAME in household_names
    # prior claim
    assert len(auto.history.accidents_and_claims) == 1
    assert auto.history.accidents_and_claims[0].fault_percentage == 25.0
    # conviction
    assert len(auto.history.convictions) == 1
    # insurance interruption
    assert len(auto.history.cancellations) == 1
    assert auto.history.current_insurance.years_continuously_insured == 0.0


# --- overrides ----------------------------------------------------------

def test_friendly_override_removes_field() -> None:
    profile = make_standard_auto_profile(annual_kilometres=None)
    assert profile.is_live_quote_ready is True  # still valid
    assert profile.product_data.vehicles[0].use.annual_kilometres is None


def test_friendly_override_changes_value() -> None:
    profile = make_standard_auto_profile(tpl_selected_limit=5_000_000)
    assert profile.product_data.coverage.third_party_liability.selected_limit == 5_000_000


def test_canonical_path_override() -> None:
    profile = make_standard_auto_profile(**{"applicant.address.city": "Ottawa"})
    assert profile.applicant.address.city == "Ottawa"
    assert profile.is_live_quote_ready is True


def test_dynamic_optional_field_via_path_override() -> None:
    """Adding/removing an optional field requires no fixture rewrite: any
    optional canonical field can be overridden by path."""
    profile = make_standard_auto_profile(
        **{"product_data.vehicles[0].use.commuting_days_per_week": 3}
    )
    assert profile.product_data.vehicles[0].use.commuting_days_per_week == 3
    # and cleared back to missing without touching persona code
    cleared = make_standard_auto_profile(
        **{"product_data.vehicles[0].use.commuting_days_per_week": None}
    )
    assert is_missing(cleared, "product_data.vehicles[0].use.commuting_days_per_week")


def test_overrides_do_not_leak_between_personas() -> None:
    overridden = make_standard_auto_profile(annual_kilometres=None)
    fresh = make_standard_auto_profile()
    assert fresh.product_data.vehicles[0].use.annual_kilometres == 12000
    assert overridden.product_data.vehicles[0].use.annual_kilometres is None


def test_unknown_friendly_override_raises() -> None:
    with pytest.raises(ValueError):
        make_standard_auto_profile(**{"not_a_real_friendly_name": 1})  # goes to path overrides
    # note: unknown CANONICAL paths raise a safe FieldPathError/ProfileUpdateError
    with pytest.raises(Exception):
        make_standard_auto_profile(**{"applicant.does_not_exist": 1})


# --- privacy / safe outputs ---------------------------------------------

def test_sensitive_values_excluded_from_redacted_output() -> None:
    for builder in (
        make_standard_auto_profile,
        make_progressive_auto_profile,
        make_edge_case_auto_profile,
    ):
        profile = builder()
        for payload in (str(profile), json.dumps(profile.redacted_dict())):
            for marker in SENSITIVE_MARKERS:
                assert marker not in payload, f"{marker!r} leaked in safe output"


def test_sensitive_values_excluded_from_repr() -> None:
    profile = make_edge_case_auto_profile()
    for marker in SENSITIVE_MARKERS:
        assert marker not in repr(profile)


def test_fixture_values_are_actually_populated() -> None:
    """Sanity: raw dumps DO contain the synthetic values; redaction is what
    removes them from safe output."""
    profile = make_edge_case_auto_profile()
    raw = json.dumps(profile.model_dump(mode="json"), default=str)
    assert "1HGCM82633A000000" in raw
    assert "synthetic minor collision at low speed" in raw


# --- reuse by Issue #5 machinery ----------------------------------------

def test_persona_reusable_with_vault_and_validated_updates() -> None:
    vault = InMemoryProfileVault()
    profile = make_progressive_auto_profile()
    profile_id = vault.create(profile)
    stored = vault.get(profile_id)
    assert stored is not None
    assert stored.is_draft is True
    # progressive intake-style validated update
    updated = stored.updated("product_data.vehicles[0].use.annual_kilometres", 12000)
    vault.update(profile_id, updated)
    assert vault.get(profile_id).product_data.vehicles[0].use.annual_kilometres == 12000


def test_friendly_paths_point_at_existing_optional_fields() -> None:
    """The friendly-name map stays valid against the schema (dynamic-field
    safety): every mapped path resolves on the standard persona."""
    profile = make_standard_auto_profile()
    for path in FRIENDLY_FIELD_PATHS.values():
        # resolving must not raise (path exists on the schema)
        from app.models.insurance.paths import resolve

        resolve(profile, path)
