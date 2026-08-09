"""Tests for the data-driven route requirement resolver (Issue #6)."""

from __future__ import annotations

from app.models.registry import MarketRegistryEntry, MarketRequirement
from app.services.route_planner.requirements import RequirementResolver

from route_planner_helpers import DEFAULT_REQUIREMENTS, entry, make_planner, write_requirements

AUTO_LICENCE = "product_data.drivers[0].licence.licence_number"
AUTO_VIN = "product_data.vehicles[0].identity.vin"


def _resolver(tmp_path, default, per_route=None):
    from app.services.route_planner.requirements import default_requirements_dir

    directory = write_requirements(tmp_path, default, per_route or {})
    return RequirementResolver(requirements_dir=directory)


def test_default_applies_to_all_entries(tmp_path) -> None:
    resolver = _resolver(tmp_path, DEFAULT_REQUIREMENTS)
    e = MarketRegistryEntry.model_validate(entry("td-insurance"))
    assert resolver.requirements_for(e) == set(DEFAULT_REQUIREMENTS)


def test_per_route_adds_paths(tmp_path) -> None:
    per_route = {"td-insurance": ["product_data.vehicles[0].use.annual_kilometres"]}
    resolver = _resolver(tmp_path, DEFAULT_REQUIREMENTS, per_route)
    td = MarketRegistryEntry.model_validate(entry("td-insurance"))
    sonnet = MarketRegistryEntry.model_validate(entry("sonnet"))
    assert "product_data.vehicles[0].use.annual_kilometres" in resolver.requirements_for(td)
    assert "product_data.vehicles[0].use.annual_kilometres" not in resolver.requirements_for(sonnet)


def test_market_requirement_enum_maps_to_paths(tmp_path) -> None:
    resolver = _resolver(tmp_path, [])
    with_licence = MarketRegistryEntry.model_validate(
        entry("route-a", requirements=["licence"])
    )
    with_vin = MarketRegistryEntry.model_validate(entry("route-b", requirements=["vin"]))
    assert resolver.requirements_for(with_licence) == {AUTO_LICENCE}
    assert resolver.requirements_for(with_vin) == {AUTO_VIN}


def test_unknown_registry_falls_back_to_default(tmp_path) -> None:
    resolver = _resolver(tmp_path, DEFAULT_REQUIREMENTS)
    unknown = MarketRegistryEntry.model_validate(entry("not-in-per-route-map"))
    assert resolver.requirements_for(unknown) == set(DEFAULT_REQUIREMENTS)


def test_requirements_are_data_driven(tmp_path) -> None:
    """Editing the data file changes requirements without touching code."""
    first = _resolver(tmp_path, DEFAULT_REQUIREMENTS)
    entry_a = MarketRegistryEntry.model_validate(entry("route-a"))
    assert "applicant.address.years_at_current_address" not in first.requirements_for(entry_a)
    # edit data only
    updated = _resolver(
        tmp_path,
        [*DEFAULT_REQUIREMENTS, "applicant.address.years_at_current_address"],
    )
    assert "applicant.address.years_at_current_address" in updated.requirements_for(entry_a)


def test_requirements_are_combined_default_plus_enum(tmp_path) -> None:
    resolver = _resolver(tmp_path, DEFAULT_REQUIREMENTS)
    entry_with_vin = MarketRegistryEntry.model_validate(entry("route-c", requirements=["vin"]))
    assert resolver.requirements_for(entry_with_vin) == set([*DEFAULT_REQUIREMENTS, AUTO_VIN])


def test_planner_resolves_requirements_via_data(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        per_route_reqs={"route-a": ["product_data.vehicles[0].use.annual_kilometres"]},
    )
    plan = planner.plan("session-1")
    assert "product_data.vehicles[0].use.annual_kilometres" in plan.routes[0].requirements
