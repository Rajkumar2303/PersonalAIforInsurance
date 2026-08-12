"""Tests for the deterministic route planner (Issue #6).

Covers product-aware routing, registry + dedup integration, per-route readiness,
multiple blockers, consent, channels, ranking, and Issue #5 missing-field
integration. All hermetic with synthetic data.
"""

from __future__ import annotations

import json

from app.models.insurance.enums import InsuranceType
from app.models.route_planner import RouteBlockerKind

from route_planner_helpers import (
    DEFAULT_REQUIREMENTS,
    StubProfileSource,
    entry,
    make_planner,
    rate_source,
)

AUTO_KM = "product_data.vehicles[0].use.annual_kilometres"


def _full_presence() -> dict[str, bool]:
    return {path: True for path in DEFAULT_REQUIREMENTS}


# --- product-aware AUTO routing -----------------------------------------

def test_non_auto_session_returns_not_applicable_plan(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("td-insurance")],
        profile_source=StubProfileSource(insurance_type=InsuranceType.HOME),
    )
    plan = planner.plan("session-home")
    assert plan.routes == []
    assert plan.insurance_type is InsuranceType.HOME


def test_auto_session_plans_routes(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("td-insurance", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["td-insurance"])],
        profile_source=StubProfileSource(presence=_full_presence(), consent={"td-insurance": True}),
    )
    plan = planner.plan("session-1")
    assert plan.insurance_type is InsuranceType.AUTO
    assert plan.summary.planned_route_count == 1
    assert plan.routes[0].is_ready is True


# --- dedup integration (rules 4-5) --------------------------------------

def test_confirmed_duplicates_grouped_into_one_route(tmp_path) -> None:
    entries = [
        entry("brand-a", distinct_rate_source_id="RS-1"),
        entry("brand-b", distinct_rate_source_id="RS-1"),
    ]
    planner = make_planner(
        tmp_path,
        entries,
        rate_sources=[rate_source("RS-1", related_registry_ids=["brand-a", "brand-b"])],
        profile_source=StubProfileSource(
            presence=_full_presence(), consent={"brand-a": True, "brand-b": True}
        ),
    )
    plan = planner.plan("session-1")
    # confirmed duplicate group -> one PRIMARY + one ALTERNATIVE, all visible
    assert plan.summary.confirmed_duplicate_groups == 1
    assert plan.summary.alternative_route_count == 1
    assert plan.summary.planned_route_count == 2
    by_id = {r.registry_id: r for r in plan.routes}
    assert set(by_id) == {"brand-a", "brand-b"}
    for route in by_id.values():
        assert route.deduplication_status == "duplicate_confirmed"
        assert set(route.group_members) == {"brand-a", "brand-b"}
    alternatives = [r for r in by_id.values() if r.is_alternative]
    primaries = [r for r in by_id.values() if not r.is_alternative]
    assert len(primaries) == 1
    assert len(alternatives) == 1


def test_possible_and_unresolved_duplicates_remain_visible(tmp_path) -> None:
    entries = [
        entry("brand-a", insurer_group="GRP-X"),
        entry("brand-b", insurer_group="GRP-X"),
    ]
    planner = make_planner(tmp_path, entries)
    plan = planner.plan("session-1")
    # same group, no verified source -> both stay as SEPARATE visible routes
    assert plan.summary.planned_route_count == 2
    registry_ids = {r.registry_id for r in plan.routes}
    assert registry_ids == {"brand-a", "brand-b"}
    # neither is suppressed; both flagged unresolved source
    assert all(
        any(b.kind is RouteBlockerKind.RATE_SOURCE_UNRESOLVED for b in r.blockers)
        for r in plan.routes
    )


# --- per-route readiness (rules 1-2) ------------------------------------

def test_readiness_is_per_route_not_global(tmp_path) -> None:
    per_route = {"route-a": [AUTO_KM]}
    presence = _full_presence()  # legal_name/postal/licence/vin all present
    # route-b requires only the defaults (all present) -> READY even though the
    # applicant is not globally live-quote ready (annual_km missing for route-a)
    planner = make_planner(
        tmp_path,
        [entry("route-a", distinct_rate_source_id="RS-1"), entry("route-b", distinct_rate_source_id="RS-2")],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["route-a"]),
            rate_source("RS-2", related_registry_ids=["route-b"]),
        ],
        per_route_reqs=per_route,
        profile_source=StubProfileSource(presence=presence, consent={"route-a": True, "route-b": True}),
    )
    plan = planner.plan("session-1")
    by_id = {r.registry_id: r for r in plan.routes}
    assert by_id["route-a"].is_ready is False  # annual_km missing
    assert any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == AUTO_KM
        for b in by_id["route-a"].blockers
    )
    assert by_id["route-b"].is_ready is True  # defaults present


# --- multiple blockers --------------------------------------------------

def test_route_can_have_multiple_simultaneous_blockers(tmp_path) -> None:
    # missing field + no consent + unresolved source -> 3 blockers
    per_route = {"route-a": [AUTO_KM]}
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        per_route_reqs=per_route,
        profile_source=StubProfileSource(presence={}),
    )
    plan = planner.plan("session-1")
    route = plan.routes[0]
    kinds = {b.kind for b in route.blockers}
    assert RouteBlockerKind.MISSING_FIELD in kinds
    assert RouteBlockerKind.CONSENT_REQUIRED in kinds
    assert RouteBlockerKind.RATE_SOURCE_UNRESOLVED in kinds
    assert len(route.blockers) >= 3


# --- consent integration ------------------------------------------------

def test_consent_blocker_cleared_when_granted(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["route-a"])],
        profile_source=StubProfileSource(presence=_full_presence(), consent={"route-a": True}),
    )
    plan = planner.plan("session-1")
    assert not any(
        b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in plan.routes[0].blockers
    )


def test_consent_blocker_present_when_not_granted(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["route-a"])],
        profile_source=StubProfileSource(presence=_full_presence(), consent={}),
    )
    plan = planner.plan("session-1")
    assert any(
        b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in plan.routes[0].blockers
    )


# --- route channels -----------------------------------------------------

def test_channels_derived_from_registry(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [
            entry(
                "route-a",
                quote_url="https://example.test/quote",
                public_phone_route="1-800-555-0199",
                callback_route="callback-tool",
                licensed_intermediary="Synthetic Broker",
            )
        ],
    )
    plan = planner.plan("session-1")
    kinds = {c.kind.value for c in plan.routes[0].channels}
    assert kinds == {"online", "phone", "callback", "broker"}


# --- deterministic ranking ----------------------------------------------

def test_deterministic_ranking_ready_first(tmp_path) -> None:
    entries = [
        entry("z-late", distinct_rate_source_id="RS-3"),
        entry("a-ready", distinct_rate_source_id="RS-1"),
        entry("m-unresolved"),
    ]
    planner = make_planner(
        tmp_path,
        entries,
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["a-ready"]),
            rate_source("RS-3", related_registry_ids=["z-late"]),
        ],
        profile_source=StubProfileSource(presence=_full_presence(), consent={e["registry_id"]: True for e in entries}),
    )
    plan = planner.plan("session-1")
    ranks = {r.registry_id: r.rank for r in plan.routes}
    # ready (resolved source) routes rank before unresolved, regardless of name
    assert ranks["a-ready"] < ranks["m-unresolved"]
    assert ranks["z-late"] < ranks["m-unresolved"]
    # ranks are 1..N and unique
    assert sorted(ranks.values()) == list(range(1, len(entries) + 1))


# --- plan is paths-only (rule 8) ----------------------------------------

def test_plan_contains_canonical_paths_not_values(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        per_route_reqs={"route-a": [AUTO_KM]},
        profile_source=StubProfileSource(presence={}),
    )
    plan = planner.plan("session-1")
    text = json.dumps(plan.model_dump(mode="json"))
    for marker in ("T0000-00000-00000", "1HGCM82633A000000", "M0A 0A0", "Test Applicant"):
        assert marker not in text
    assert AUTO_KM in text  # paths present


def test_required_missing_paths_aggregated(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a"), entry("route-b")],
        per_route_reqs={"route-a": [AUTO_KM], "route-b": ["applicant.address.years_at_current_address"]},
        profile_source=StubProfileSource(presence={}),
    )
    plan = planner.plan("session-1")
    assert AUTO_KM in plan.required_missing_paths
    assert "applicant.address.years_at_current_address" in plan.required_missing_paths


# --- Issue #5 missing-field integration ---------------------------------

def test_request_missing_fields_integrates_with_intake(tmp_path) -> None:
    source = StubProfileSource(presence={})
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        per_route_reqs={"route-a": [AUTO_KM]},
        profile_source=source,
    )
    outcomes = planner.request_missing_fields("session-1")
    assert len(source.requests) == 1
    requested_paths, context = source.requests[0]
    assert AUTO_KM in requested_paths
    assert context == "route_planner"
    assert outcomes == source._request_results
