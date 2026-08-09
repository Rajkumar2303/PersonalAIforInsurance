"""Tests for the data-driven intake field catalog (Issue #5)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models.intake.field_catalog import IntakeFieldDefinition, IntakePhase
from app.services.intake.catalog import CatalogLoadError, IntakeFieldCatalog

from intake_helpers import make_field, standard_fields, write_catalog


def _catalog(tmp_path):
    return IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, standard_fields()))


def test_catalog_loads_all_definitions(tmp_path) -> None:
    catalog = _catalog(tmp_path)
    assert len(catalog.all()) == len(standard_fields())


def test_template_placeholder_resolves_to_index_zero(tmp_path) -> None:
    catalog = _catalog(tmp_path)
    field = catalog.get("vehicle_annual_km")
    assert field is not None
    assert (
        catalog.resolve_template(field.canonical_path_template)
        == "product_data.vehicles[0].use.annual_kilometres"
    )


def test_reverse_lookup_by_concrete_path(tmp_path) -> None:
    catalog = _catalog(tmp_path)
    field = catalog.by_path("product_data.vehicles[0].use.annual_kilometres")
    assert field is not None
    assert field.field_id == "vehicle_annual_km"


def test_container_path_derived_from_template(tmp_path) -> None:
    catalog = _catalog(tmp_path)
    field = catalog.get("vehicle_vin")
    assert catalog.container_path(field) == "product_data.vehicles"


def test_phase_filtering(tmp_path) -> None:
    catalog = _catalog(tmp_path)
    starter = catalog.for_phase(catalog.all()[0].product_type, IntakePhase.STARTER)
    route = catalog.for_phase(catalog.all()[0].product_type, IntakePhase.ROUTE_SPECIFIC)
    assert starter and route
    assert {f.field_id for f in starter} >= {
        "legal_name",
        "postal_code",
        "vehicle_vin",
        "driver_licence_number",
    }
    assert "vehicle_annual_km" in {f.field_id for f in route}


def test_disabled_field_excluded(tmp_path) -> None:
    fields = standard_fields()
    fields.append(make_field("disabled_thing", "applicant.contact.email", enabled=False))
    catalog = IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, fields))
    assert catalog.get("disabled_thing") is not None
    assert all(f.field_id != "disabled_thing" for f in catalog.enabled(catalog.all()[0].product_type))


def test_duplicate_field_id_fails(tmp_path) -> None:
    fields = standard_fields()
    fields.append(make_field("legal_name", "applicant.identity.alias"))
    with pytest.raises(CatalogLoadError):
        IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, fields))


def test_invalid_record_fails(tmp_path) -> None:
    catalog_dir = tmp_path / "intake"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / "bad.json").write_text(
        json.dumps({"fields": [{"field_id": "x", "canonical_path_template": 5}]}), encoding="utf-8"
    )
    with pytest.raises(CatalogLoadError):
        IntakeFieldCatalog(catalog_dir=catalog_dir)


def test_definition_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        IntakeFieldDefinition.model_validate(
            make_field("x", "applicant.identity.alias", unexpected_field="boom")
        )


def test_seed_fields_detected(tmp_path) -> None:
    catalog = _catalog(tmp_path)
    seed_ids = {f.field_id for f in catalog.seed_fields(catalog.all()[0].product_type)}
    assert seed_ids == {"legal_name", "postal_code"}


def test_trace_metadata_counts_only(tmp_path) -> None:
    catalog = _catalog(tmp_path)
    meta = catalog.trace_metadata()
    assert meta["field_count"] == len(standard_fields())
    assert set(meta) >= {"field_count", "enabled_count", "starter_count", "route_specific_count", "sensitive_late_count"}
