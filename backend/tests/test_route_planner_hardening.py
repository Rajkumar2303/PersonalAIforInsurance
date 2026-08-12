"""Issue #6 part 2 - route planner hardening tests.

Covers the mandatory scenarios: route-level completeness, multiple blockers,
progressive Issue #5 integration, ask-once, duplicate primary/alternative,
non-suppression of possible/unresolved duplicates, same-group no-dedup, dynamic
market/field/dedup changes, consent + household-driver consent, all channels,
unknown-requirement safety, coverage metrics, and privacy.
"""

from __future__ import annotations

import inspect
import json

from app.models.insurance.enums import InsuranceType
from app.models.route_planner import RouteBlockerKind

from route_planner_helpers import (
    DEFAULT_REQUIREMENTS,
    StubProfileSource,
    complete_starter,
    entry,
    make_integration_env,
    make_planner,
    rate_source,
    write_registry,
    write_requirements,
)

AUTO_KM = "product_data.vehicles[0].use.annual_kilometres"
VIN = "product_data.vehicles[0].identity.vin"
POSTAL = "applicant.address.postal_code"
OTHER_DRIVER = "product_data.drivers[0].other_drivers[0].name"


# --- scenario 1: route-level completeness -------------------------------

def test_route_level_completeness_postal_vs_vin(tmp_path) -> None:
    """Route A needs postal only; Route B needs VIN; VIN missing -> A ready,
    B blocked by missing VIN."""
    planner = make_planner(
        tmp_path,
        [
            entry("route-a", distinct_rate_source_id="RS-1"),
            entry("route-b", distinct_rate_source_id="RS-2"),
        ],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["route-a"]),
            rate_source("RS-2", related_registry_ids=["route-b"]),
        ],
        default_reqs=[],
        per_route_reqs={"route-a": [POSTAL], "route-b": [VIN]},
        profile_source=StubProfileSource(
            presence={POSTAL: True, VIN: False},  # VIN missing
            consent={"route-a": True, "route-b": True},
        ),
    )
    plan = planner.plan("session-1")
    by_id = {r.registry_id: r for r in plan.routes}
    assert by_id["route-a"].is_ready is True
    assert by_id["route-b"].is_ready is False
    assert any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == VIN
        for b in by_id["route-b"].blockers
    )


# --- scenario 2: multiple simultaneous blockers -------------------------

def test_multiple_blockers_vin_consent_membership_preserved(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a", requirements=["membership"])],
        default_reqs=[],
        per_route_reqs={"route-a": [VIN]},
        profile_source=StubProfileSource(presence={VIN: False}, consent={}),
    )
    plan = planner.plan("session-1")
    kinds = {b.kind for b in plan.routes[0].blockers}
    assert kinds == {
        RouteBlockerKind.MISSING_FIELD,
        RouteBlockerKind.CONSENT_REQUIRED,
        RouteBlockerKind.AFFINITY_RESTRICTED,
        RouteBlockerKind.RATE_SOURCE_UNRESOLVED,
    }


# --- scenario 3: progressive Issue #5 integration -----------------------

def test_progressive_missing_field_resolution(tmp_path) -> None:
    """Missing field -> collect through IntakeEngine -> re-plan -> blocker
    disappears."""
    engine, planner = make_integration_env(
        tmp_path,
        [
            entry("route-a", distinct_rate_source_id="RS-1"),
            entry("route-b", distinct_rate_source_id="RS-2"),
        ],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["route-a"]),
            rate_source("RS-2", related_registry_ids=["route-b"]),
        ],
        per_route_reqs={"route-a": [AUTO_KM]},
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    complete_starter(engine, sid)
    engine.grant_route_consent(sid, "route-a", [], True)
    engine.grant_route_consent(sid, "route-b", [], True)

    plan = planner.plan(sid)
    by_id = {r.registry_id: r for r in plan.routes}
    assert by_id["route-a"].is_ready is False
    assert by_id["route-b"].is_ready is True  # defaults present, draft still fine

    # collect the missing field through the intake engine
    assert engine.submit_answer(sid, AUTO_KM, 12000).validation_success is True

    plan2 = planner.plan(sid)
    assert plan2.summary.missing_field_paths_count == 0
    assert {r.registry_id for r in plan2.routes if r.is_ready} == {"route-a", "route-b"}


# --- scenario 4: ask-once ------------------------------------------------

def test_ask_once_two_routes_reuse_same_field(tmp_path) -> None:
    engine, planner = make_integration_env(
        tmp_path,
        [
            entry("route-a", distinct_rate_source_id="RS-1"),
            entry("route-c", distinct_rate_source_id="RS-3"),
        ],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["route-a"]),
            rate_source("RS-3", related_registry_ids=["route-c"]),
        ],
        per_route_reqs={"route-a": [AUTO_KM], "route-c": [AUTO_KM]},
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    complete_starter(engine, sid)
    engine.grant_route_consent(sid, "route-a", [], True)
    engine.grant_route_consent(sid, "route-c", [], True)

    # collect once
    engine.submit_answer(sid, AUTO_KM, 12000)
    plan = planner.plan(sid)
    assert plan.summary.missing_field_paths_count == 0
    assert {r.registry_id for r in plan.routes if r.is_ready} == {"route-a", "route-c"}

    # second route / another caller sees it as already known -> no re-ask
    outcomes = engine.request_fields(sid, [AUTO_KM], "aviva")
    assert outcomes[0].already_known is True
    assert planner.request_missing_fields(sid) == []  # nothing missing to ask


# --- scenario 5: duplicate primary/alternative ---------------------------

def test_duplicate_primary_and_alternative(tmp_path) -> None:
    entries = [
        entry("brand-a", distinct_rate_source_id="RS-1", quote_url="https://a.test"),
        entry("brand-b", distinct_rate_source_id="RS-1", quote_url="https://b.test"),
    ]
    planner = make_planner(
        tmp_path,
        entries,
        rate_sources=[rate_source("RS-1", related_registry_ids=["brand-a", "brand-b"])],
        profile_source=StubProfileSource(
            presence={p: True for p in DEFAULT_REQUIREMENTS}, consent={"brand-a": True, "brand-b": True}
        ),
    )
    plan = planner.plan("session-1")
    by_id = {r.registry_id: r for r in plan.routes}
    primary = [r for r in by_id.values() if not r.is_alternative]
    alternatives = [r for r in by_id.values() if r.is_alternative]
    assert len(primary) == 1 and primary[0].is_ready
    assert len(alternatives) == 1 and alternatives[0].is_ready
    assert {r.registry_id for r in plan.routes} == {"brand-a", "brand-b"}
    assert all(r.distinct_rate_source_id == "RS-1" for r in by_id.values())
    assert all(r.deduplication_status == "duplicate_confirmed" for r in by_id.values())


# --- scenarios 6 & 7: never suppress / never auto-dedup same group -------

def test_possible_unresolved_never_suppressed_and_same_group_never_deduped(tmp_path) -> None:
    entries = [entry("twin-a", insurer_group="GRP-X"), entry("twin-b", insurer_group="GRP-X")]
    planner = make_planner(tmp_path, entries)
    plan = planner.plan("session-1")
    assert plan.summary.planned_route_count == 2
    assert plan.summary.confirmed_duplicate_groups == 0
    assert plan.summary.alternative_route_count == 0
    registry_ids = {r.registry_id for r in plan.routes}
    assert registry_ids == {"twin-a", "twin-b"}
    assert all(not r.is_alternative for r in plan.routes)
    assert all(
        any(b.kind is RouteBlockerKind.RATE_SOURCE_UNRESOLVED for b in r.blockers)
        for r in plan.routes
    )


# --- scenario 8: dynamic market changes via data -------------------------

def test_dynamic_market_changes_via_data(tmp_path) -> None:
    from app.services.deduplication import RateSourceDeduplicationService
    from app.services.market_registry import MarketRegistryService
    from app.services.route_planner.planner import RoutePlanner
    from app.services.route_planner.requirements import RequirementResolver

    # first market state
    entries = [entry("route-a", quote_url="https://a.example.test", distinct_rate_source_id="RS-1")]
    write_registry(tmp_path, entries)
    write_requirements(tmp_path, DEFAULT_REQUIREMENTS, {"route-a": [AUTO_KM]})
    registry = MarketRegistryService(registry_dir=tmp_path / "reg")
    dedup = RateSourceDeduplicationService(registry_service=registry, rate_sources_dir=tmp_path / "rs")
    requirements = RequirementResolver(requirements_dir=tmp_path / "routes")
    planner1 = RoutePlanner(
        registry=registry, dedup=dedup, requirements=requirements, profile_source=StubProfileSource()
    )
    plan1 = planner1.plan("session-1")
    assert plan1.routes[0].channels[0].value == "https://a.example.test"
    assert AUTO_KM in plan1.routes[0].requirements

    # DATA-ONLY market change: add a market, change channel, change requirements
    write_registry(
        tmp_path,
        [
            entry("route-a", quote_url="https://b.example.test", distinct_rate_source_id="RS-1"),
            entry("route-new", distinct_rate_source_id="RS-2"),
        ],
    )
    write_requirements(tmp_path, DEFAULT_REQUIREMENTS, {"route-a": [POSTAL]})
    registry2 = MarketRegistryService(registry_dir=tmp_path / "reg")
    dedup2 = RateSourceDeduplicationService(registry_service=registry2, rate_sources_dir=tmp_path / "rs")
    requirements2 = RequirementResolver(requirements_dir=tmp_path / "routes")
    planner2 = RoutePlanner(
        registry=registry2, dedup=dedup2, requirements=requirements2, profile_source=StubProfileSource()
    )
    plan2 = planner2.plan("session-1")
    by_id = {r.registry_id: r for r in plan2.routes}
    assert "route-new" in by_id
    assert by_id["route-a"].channels[0].value == "https://b.example.test"
    assert AUTO_KM not in by_id["route-a"].requirements
    assert POSTAL in by_id["route-a"].requirements


# --- scenario 9: dynamic canonical field, no planner code ----------------

def test_dynamic_canonical_field_needs_no_planner_code(tmp_path) -> None:
    new_field = "applicant.identity.gender"  # optional canonical field (schema)
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        default_reqs=[],
        per_route_reqs={"route-a": [new_field]},
        profile_source=StubProfileSource(presence={}),
    )
    plan = planner.plan("session-1")
    assert any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == new_field
        for b in plan.routes[0].blockers
    )
    # planner source has NO special-case for this field
    import app.services.route_planner.planner as planner_module

    assert "gender" not in inspect.getsource(planner_module)
    # once present -> no blocker
    planner2 = make_planner(
        tmp_path,
        [entry("route-a", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["route-a"])],
        default_reqs=[],
        per_route_reqs={"route-a": [new_field]},
        profile_source=StubProfileSource(presence={new_field: True}, consent={"route-a": True}),
    )
    assert planner2.plan("session-1").routes[0].is_ready is True


# --- scenario 10: dedup merge/split via data -----------------------------

def test_dedup_merge_then_split_via_data(tmp_path) -> None:
    from app.services.deduplication import RateSourceDeduplicationService
    from app.services.market_registry import MarketRegistryService
    from app.services.route_planner.planner import RoutePlanner
    from app.services.route_planner.requirements import RequirementResolver

    # MERGE: both routes share RS-1
    write_registry(
        tmp_path,
        [
            entry("brand-a", distinct_rate_source_id="RS-1"),
            entry("brand-b", distinct_rate_source_id="RS-1"),
        ],
    )
    merged_registry = MarketRegistryService(registry_dir=tmp_path / "reg")
    merged = RoutePlanner(
        registry=merged_registry,
        dedup=RateSourceDeduplicationService(registry_service=merged_registry, rate_sources_dir=tmp_path / "rs"),
        requirements=RequirementResolver(requirements_dir=tmp_path / "routes"),
        profile_source=StubProfileSource(),
    )
    plan_merged = merged.plan("session-1")
    assert plan_merged.summary.confirmed_duplicate_groups == 1
    assert plan_merged.summary.alternative_route_count == 1

    # SPLIT: brand-b now maps to RS-2 (data only)
    write_registry(
        tmp_path,
        [
            entry("brand-a", distinct_rate_source_id="RS-1"),
            entry("brand-b", distinct_rate_source_id="RS-2"),
        ],
    )
    split_registry = MarketRegistryService(registry_dir=tmp_path / "reg")
    split = RoutePlanner(
        registry=split_registry,
        dedup=RateSourceDeduplicationService(registry_service=split_registry, rate_sources_dir=tmp_path / "rs"),
        requirements=RequirementResolver(requirements_dir=tmp_path / "routes"),
        profile_source=StubProfileSource(),
    )
    plan_split = split.plan("session-1")
    assert plan_split.summary.confirmed_duplicate_groups == 0
    assert plan_split.summary.alternative_route_count == 0
    assert {r.distinct_rate_source_id for r in plan_split.routes} == {"RS-1", "RS-2"}


# --- scenario 11: consent granted/denied + household-driver consent ------

def test_route_consent_granted_denied(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [
            entry("granted", distinct_rate_source_id="RS-1"),
            entry("denied", distinct_rate_source_id="RS-2"),
        ],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["granted"]),
            rate_source("RS-2", related_registry_ids=["denied"]),
        ],
        profile_source=StubProfileSource(
            presence={p: True for p in DEFAULT_REQUIREMENTS},
            consent={"granted": True, "denied": False},
        ),
    )
    plan = planner.plan("session-1")
    by_id = {r.registry_id: r for r in plan.routes}
    assert by_id["granted"].is_ready is True
    assert by_id["denied"].is_ready is False
    assert any(b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in by_id["denied"].blockers)


def test_household_driver_consent_gate_integration(tmp_path) -> None:
    engine, planner = make_integration_env(
        tmp_path,
        [entry("route-household")],
        default_reqs=[],
        per_route_reqs={"route-household": [OTHER_DRIVER]},
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    complete_starter(engine, sid)

    # planner surfaces household-consent blocker before attestation
    plan = planner.plan(sid)
    kinds = {b.kind for b in plan.routes[0].blockers}
    assert RouteBlockerKind.HUMAN_REQUIRED in kinds
    assert RouteBlockerKind.MISSING_FIELD in kinds

    # intake refuses to collect before attestation
    outcomes = planner.request_missing_fields(sid)
    assert outcomes[0].consent_required is True
    assert outcomes[0].human_checkpoint_required is True

    # grant attestation -> gate clears (field still missing -> missing_field stays)
    engine.record_household_driver_consent(sid, "driver_1")
    plan2 = planner.plan(sid)
    kinds2 = {b.kind for b in plan2.routes[0].blockers}
    assert RouteBlockerKind.HUMAN_REQUIRED not in kinds2
    assert RouteBlockerKind.MISSING_FIELD in kinds2
    outcomes2 = planner.request_missing_fields(sid)
    assert outcomes2[0].consent_required is False


# --- scenario 12: all channel kinds -------------------------------------

def test_channels_web_phone_callback_human_broker(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [
            entry(
                "route-full",
                quote_url="https://full.example.test",
                public_phone_route="1-800-555-0100",
                callback_route="callback-tool",
                licensed_intermediary="Synthetic Broker",
                requirements=["human"],
            ),
            entry("route-phone", public_phone_route="1-800-555-0101"),
            entry("route-discovery"),
        ],
    )
    plan = planner.plan("session-1")
    by_id = {r.registry_id: r for r in plan.routes}
    full_kinds = {c.kind.value for c in by_id["route-full"].channels}
    assert {"online", "phone", "callback", "broker", "human"} <= full_kinds
    assert {c.kind.value for c in by_id["route-phone"].channels} == {"phone"}
    assert {c.kind.value for c in by_id["route-discovery"].channels} == {"discovery_only"}


# --- scenario 13: unknown requirement fails safely -----------------------

def test_unknown_requirement_path_fails_safely(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        default_reqs=[],
        per_route_reqs={"route-a": ["applicant.does_not_exist.field"]},
        profile_source=StubProfileSource(presence={}),
    )
    plan = planner.plan("session-1")  # must not crash
    assert any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == "applicant.does_not_exist.field"
        for b in plan.routes[0].blockers
    )
    json.dumps(plan.model_dump(mode="json"))  # serializes fine


def test_unknown_market_requirement_enum_ignored_safely(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a", requirements=["other"])],  # OTHER has no mapping
        profile_source=StubProfileSource(presence={}),
    )
    plan = planner.plan("session-1")
    assert not any(b.kind is RouteBlockerKind.OTHER for b in plan.routes[0].blockers)
    assert plan.routes[0].blockers  # other blockers still present (missing/consent/unresolved)


# --- coverage metrics ----------------------------------------------------

def test_coverage_metrics_summary(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [
            entry("ready-a", distinct_rate_source_id="RS-1"),
            entry("dup-a", distinct_rate_source_id="RS-2"),
            entry("dup-b", distinct_rate_source_id="RS-2"),
            entry("unresolved-c"),
        ],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["ready-a"]),
            rate_source("RS-2", related_registry_ids=["dup-a", "dup-b"]),
        ],
        profile_source=StubProfileSource(
            presence={p: True for p in DEFAULT_REQUIREMENTS},
            consent={e["registry_id"]: True for e in [
                {"registry_id": "ready-a"}, {"registry_id": "dup-a"}, {"registry_id": "dup-b"}
            ]},
        ),
    )
    plan = planner.plan("session-1")
    s = plan.summary
    assert s.raw_registry_count == 4
    assert s.planned_route_count == 4  # ready-a + dup primary + dup alt + unresolved
    assert s.confirmed_duplicate_groups == 1
    assert s.alternative_route_count == 1
    assert s.unresolved_rate_sources == 1
    assert s.ready_count >= 1
    assert s.blocked_count >= 1


# --- scenario 14: privacy (integration, real profile) -------------------

def test_privacy_real_profile_plan_state_logs(tmp_path, caplog) -> None:
    import logging

    engine, planner = make_integration_env(
        tmp_path,
        [entry("route-a")],
        default_reqs=[],
        per_route_reqs={"route-a": [AUTO_KM]},
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    complete_starter(engine, sid)
    engine.submit_answer(sid, "applicant.identity.date_of_birth", "1990-01-01")
    engine.submit_answer(sid, AUTO_KM, 12000)

    with caplog.at_level(logging.INFO):
        plan = planner.plan(sid)
        from app.graph.route_planner_workflow import build_route_planner_workflow
        state = build_route_planner_workflow(planner).invoke({"entry": "plan", "session_id": sid})

    for label, payload in (
        ("plan", json.dumps(plan.model_dump(mode="json"))),
        ("graph state", json.dumps(state, default=str)),
        ("logs", caplog.text),
        ("repr", repr(plan)),
    ):
        for marker in ("T0000-00000-00000", "1HGCM82633A000000", "M0A 0A0", "Test Applicant", "1990-01-01"):
            assert marker not in payload, f"{marker!r} leaked in {label}"
