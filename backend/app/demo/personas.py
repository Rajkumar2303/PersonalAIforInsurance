"""Canonical synthetic demo persona (Issue #8.5).

The single backend source of the clearly-fictional STANDARD COMPLETE persona
used to populate the local web demo form. Reusable by tests. Values are
synthetic canonical ``path -> value`` pairs matching the AUTO intake catalog
(after index-0 template resolution). This is the ONLY frontend-visible persona
source - the browser/Playwright demos and Issue #7/8 test personas remain as
they were.

Privacy: these values are obviously fictional ("Test Applicant", M0A 0A0,
T0000-...). They are returned by the dev/mock-only persona endpoint and are
NEVER treated as real applicant data. Real applicant values must never flow
through this module.
"""

from __future__ import annotations

from typing import Any

# Friendly key -> canonical path map (mirrors tests/personas.py FRIENDLY_FIELD_PATHS
# so demo values target the same canonical fields the catalog exposes).
_FRIENDLY_TO_PATH: dict[str, str] = {
    "legal_name": "applicant.identity.legal_name",
    "postal_code": "applicant.address.postal_code",
    "driver_name_on_licence": "product_data.drivers[0].licence.name_on_licence",
    "driver_licence_number": "product_data.drivers[0].licence.licence_number",
    "driver_licence_expiry": "product_data.drivers[0].licence.expiry_date",
    "vehicle_vin": "product_data.vehicles[0].identity.vin",
    "vehicle_year": "product_data.vehicles[0].identity.model_year",
    "vehicle_make": "product_data.vehicles[0].identity.make",
    "vehicle_model": "product_data.vehicles[0].identity.model",
    "date_of_birth": "applicant.identity.date_of_birth",
    "street": "applicant.address.street",
    "city": "applicant.address.city",
    "years_at_current_address": "applicant.address.years_at_current_address",
    "vehicle_annual_km": "product_data.vehicles[0].use.annual_kilometres",
    "vehicle_winter_tires": "product_data.vehicles[0].risk.winter_tires",
    "vehicle_carpool": "product_data.vehicles[0].use.carpool",
    "one_way_commute_km": "product_data.vehicles[0].use.one_way_commute_distance_km",
    "tpl_selected_limit": "product_data.coverage.third_party_liability.selected_limit",
}

# Clearly-synthetic values (all fictional; validate against the AUTO catalog:
# licence matches the Ontario pattern, VIN is exactly 17 chars, dates ISO).
STANDARD_AUTO_PERSONA: dict[str, Any] = {
    "legal_name": "Test Applicant",
    "postal_code": "M0A 0A0",
    "driver_name_on_licence": "Test Applicant",
    "driver_licence_number": "T0000-00000-00000",
    "driver_licence_expiry": "2030-12-31",
    "vehicle_vin": "1HGCM82633A000000",
    "vehicle_year": 2022,
    "vehicle_make": "TestMake",
    "vehicle_model": "TestModel",
    "date_of_birth": "1990-01-01",
    "street": "123 Test Street",
    "city": "Testville",
    "years_at_current_address": 4,
    "vehicle_annual_km": 12000,
    "vehicle_winter_tires": True,
    "vehicle_carpool": False,
    "one_way_commute_km": 18,
    "tpl_selected_limit": 2000000,
}


def standard_auto_persona() -> dict[str, Any]:
    """Return the canonical synthetic persona as ``canonical_path -> value``.

    A fresh dict is returned each call so callers can mutate it freely.
    """
    return {_FRIENDLY_TO_PATH[key]: value for key, value in STANDARD_AUTO_PERSONA.items()}


def persona_friendly_values() -> dict[str, Any]:
    """Return the persona keyed by the catalog-friendly names (for tests/UI)."""
    return dict(STANDARD_AUTO_PERSONA)
