"""Shared helpers for Issue #5 intake tests.

All intake tests use a small, fully-synthetic data-driven catalog in a temp
directory - hermetic, deterministic, and easy to mutate for the dynamic-change
scenarios.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

SYNTHETIC_LEGAL_NAME = "Test Applicant"
SYNTHETIC_LICENCE = "T0000-00000-00000"
SYNTHETIC_VIN = "1HGCM82633A000000"
SYNTHETIC_POSTAL = "M0A 0A0"
SYNTHETIC_STREET = "123 Test Street"
SYNTHETIC_CITY = "Testville"
SYNTHETIC_DOB = "1990-01-01"
SYNTHETIC_EXPIRY = "2030-12-31"
SYNTHETIC_EMAIL = "applicant.test@example.com"

# Sensitive synthetic values that must NEVER appear in traces/logs/receipts.
SENSITIVE_MARKERS = [
    SYNTHETIC_LICENCE,
    SYNTHETIC_VIN,
    SYNTHETIC_DOB,
    SYNTHETIC_STREET,
    "1990",
    "1HGCM82633A000000",
]


def make_field(field_id: str, path: str, **overrides: object) -> dict:
    """Build one catalog record with safe defaults + overrides."""
    base: dict = {
        "field_id": field_id,
        "product_type": "auto",
        "canonical_path_template": path,
        "question": f"Question for {field_id}",
        "short_label": field_id,
        "input_type": "text",
        "sensitivity": "personal",
        "collection_group": "other",
        "intake_phase": "route_specific",
        "priority": 100,
        "enabled": True,
    }
    base.update(overrides)
    return base


def standard_fields() -> list[dict]:
    """The standard synthetic AUTO catalog used by most engine tests."""
    return [
        make_field(
            "legal_name",
            "applicant.identity.legal_name",
            intake_phase="starter",
            priority=10,
            seed_required=True,
        ),
        make_field(
            "postal_code",
            "applicant.address.postal_code",
            intake_phase="starter",
            priority=20,
            seed_required=True,
            input_type="postal_code",
            sensitivity="sensitive",
        ),
        make_field(
            "driver_name_on_licence",
            "product_data.drivers[{driver_index}].licence.name_on_licence",
            intake_phase="starter",
            priority=25,
            sensitivity="sensitive",
            item_unit="driver",
            item_index_placeholder="driver_index",
            item_unit_required=True,
        ),
        make_field(
            "driver_licence_number",
            "product_data.drivers[{driver_index}].licence.licence_number",
            intake_phase="starter",
            priority=35,
            input_type="licence",
            sensitivity="sensitive",
            item_unit="driver",
            item_index_placeholder="driver_index",
            item_unit_required=True,
        ),
        make_field(
            "driver_licence_expiry",
            "product_data.drivers[{driver_index}].licence.expiry_date",
            intake_phase="starter",
            priority=45,
            input_type="date",
            sensitivity="sensitive",
            item_unit="driver",
            item_index_placeholder="driver_index",
            item_unit_required=True,
        ),
        make_field(
            "vehicle_vin",
            "product_data.vehicles[{vehicle_index}].identity.vin",
            intake_phase="starter",
            priority=30,
            input_type="vin",
            sensitivity="sensitive",
            item_unit="vehicle",
            item_index_placeholder="vehicle_index",
        ),
        make_field(
            "vehicle_year",
            "product_data.vehicles[{vehicle_index}].identity.model_year",
            intake_phase="starter",
            priority=40,
            input_type="integer",
            item_unit="vehicle",
            item_index_placeholder="vehicle_index",
            item_unit_required=True,
        ),
        make_field(
            "vehicle_make",
            "product_data.vehicles[{vehicle_index}].identity.make",
            intake_phase="starter",
            priority=50,
            item_unit="vehicle",
            item_index_placeholder="vehicle_index",
            item_unit_required=True,
        ),
        make_field(
            "vehicle_model",
            "product_data.vehicles[{vehicle_index}].identity.model",
            intake_phase="starter",
            priority=60,
            item_unit="vehicle",
            item_index_placeholder="vehicle_index",
            item_unit_required=True,
        ),
        make_field(
            "years_at_current_address",
            "applicant.address.years_at_current_address",
            intake_phase="route_specific",
            priority=10,
            input_type="years",
        ),
        make_field(
            "vehicle_annual_km",
            "product_data.vehicles[{vehicle_index}].use.annual_kilometres",
            intake_phase="route_specific",
            priority=20,
            input_type="integer",
        ),
        make_field(
            "date_of_birth",
            "applicant.identity.date_of_birth",
            intake_phase="sensitive_late",
            priority=10,
            input_type="date",
            sensitivity="sensitive",
        ),
        make_field(
            "other_driver_name",
            "product_data.drivers[{driver_index}].other_drivers[0].name",
            intake_phase="route_specific",
            priority=30,
            collection_group="household",
            sensitivity="sensitive",
            household_attestation_required=True,
        ),
        # New optional dynamic fields (Issue #7 Prompt 2): only schema + catalog
        # + browser binding + tests are required - never a BrowserExecutor change.
        make_field(
            "vehicle_commuting",
            "product_data.vehicles[{vehicle_index}].use.commuting",
            intake_phase="route_specific",
            priority=40,
            input_type="boolean",
        ),
        make_field(
            "vehicle_rideshare",
            "product_data.vehicles[{vehicle_index}].special_use.rideshare",
            intake_phase="route_specific",
            priority=45,
            input_type="boolean",
        ),
        make_field(
            "vehicle_rideshare_hours",
            "product_data.vehicles[{vehicle_index}].use.rideshare_hours_per_week",
            intake_phase="route_specific",
            priority=50,
            input_type="float",
        ),
        make_field(
            "coverage_tpl_limit",
            "product_data.coverage.third_party_liability.selected_limit",
            intake_phase="route_specific",
            priority=55,
            input_type="integer",
        ),
    ]


def write_catalog(tmp_path: Path, fields: list[dict]) -> Path:
    catalog_dir = tmp_path / "intake"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / "auto_fields.json").write_text(
        json.dumps({"fields": fields}), encoding="utf-8"
    )
    return catalog_dir


def make_engine(
    tmp_path: Path,
    fields: Optional[list[dict]] = None,
    vault=None,
    consent=None,
    registry=None,
    sessions=None,
):
    """Build an IntakeEngine over a temp synthetic catalog."""
    from app.services.intake.catalog import IntakeFieldCatalog
    from app.services.intake.consent import ConsentService
    from app.services.intake.engine import IntakeEngine
    from app.services.intake.session_store import InMemorySessionStore
    from app.services.intake.vault import InMemoryProfileVault
    from app.services.market_registry import MarketRegistryService

    catalog = IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, fields or standard_fields()))
    return IntakeEngine(
        catalog=catalog,
        vault=vault or InMemoryProfileVault(),
        sessions=sessions or InMemorySessionStore(),
        consent=consent or ConsentService(),
        registry=registry or MarketRegistryService(),
    )


def seed_profile(engine, session_id: str) -> None:
    """Answer the two seed fields so the vault profile is materialized."""
    result = engine.submit_answer(session_id, "applicant.identity.legal_name", SYNTHETIC_LEGAL_NAME)
    assert result.validation_success
    result = engine.submit_answer(session_id, "applicant.address.postal_code", SYNTHETIC_POSTAL)
    assert result.validation_success
