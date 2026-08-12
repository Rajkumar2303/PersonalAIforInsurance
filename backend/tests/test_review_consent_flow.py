"""Review & Consent flow fixes - hermetic backend tests.

Covers the contract the Review & Consent UI depends on, plus the VIN-optional
intake change:

- VIN blank passes base schema/engine validation and is never globally missing.
- Per-route VIN requirements (registry/route data) block ONLY the routes that
  declare them; routes that do not require VIN stay eligible.
- Consent -> route readiness -> "Compare eligible" (mirrors the backend
  comparison-run gate AND the frontend eligibility/button predicate).
- Consent revocation re-blocks the route.
- Mock comparison is isolated from LIVE configuration.
- No applicant values leak into plans/URLs.

No real insurers, no LLM, no browser. Uses synthetic registries plus the real
Square One registry entry (verified, RS-ZURICH-AUTO / Zurich).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.models.browser.session import (
    BrowserRefusalReason,
    BrowserStartRefusal,
    LiveExecutionGate,
)
from app.models.insurance.auto.vehicle import VehicleIdentity
from app.models.insurance.enums import InsuranceType
from app.models.route_planner import RouteBlockerKind, RouteChannelKind

from intake_helpers import (
    SENSITIVE_MARKERS,
    SYNTHETIC_LICENCE,
    SYNTHETIC_LEGAL_NAME,
    SYNTHETIC_VIN,
    seed_profile,
)
from route_planner_helpers import (
    StubProfileSource,
    entry,
    make_integration_env,
    make_planner,
    rate_source,
)

LEGAL = "applicant.identity.legal_name"
POSTAL = "applicant.address.postal_code"
LICENCE = "product_data.drivers[0].licence.licence_number"
VIN = "product_data.vehicles[0].identity.vin"


def _square_one_entry() -> dict:
    """The real verified Square One registry entry (Zurich / RS-ZURICH-AUTO)."""
    return {
        "registry_id": "square-one",
        "product_type": "auto",
        "legal_underwriter": "Zurich Insurance Company Ltd (Canadian Branch)",
        "insurer_group": "Zurich",
        "brand_or_program": "Square One",
        "distribution_type": "direct",
        "product_scope": "standard_PPA",
        "distinct_rate_source_id": "RS-ZURICH-AUTO",
        "requirements": ["licence", "vin"],
        "status": "verified",
        "last_verified_at": "2026-08-12T18:09:43.298460+00:00",
        "source_url": "https://www.squareone.ca/auto-insurance/",
        "quote_url": "https://www.squareone.ca/auto-insurance/",
        "active": True,
    }


def _square_one_planner(tmp_path, *, consent):
    """Planner over a synthetic registry containing the real Square One entry."""
    return make_planner(
        tmp_path,
        [_square_one_entry()],
        rate_sources=[rate_source("RS-ZURICH-AUTO", related_registry_ids=["square-one"])],
        profile_source=StubProfileSource(
            presence={LEGAL: True, POSTAL: True, LICENCE: True, VIN: True},
            consent=consent,
        ),
    )


def _square_one_route(plan):
    return next(r for r in plan.routes if r.registry_id == "square-one")


def _frontend_eligible(route) -> bool:
    """Mirror the ReviewConsent `isEligible` predicate: an online channel AND
    (ready OR blocked only by route-disclosure consent - so the user can grant
    it on the screen). Never bypasses or auto-grants consent."""
    channels = [c.kind.value for c in route.channels]
    blockers = [b.kind.value for b in route.blockers]
    if "online" not in channels:
        return False
    if route.is_ready:
        return True
    return bool(blockers) and all(b == "consent_required" for b in blockers)


def _run_would_execute(route, consent_granted: bool) -> bool:
    """Mirror ComparisonRunService._run_routes: route is_ready + an online
    channel + route-disclosure consent granted. This is the authoritative
    'Compare actually produces quotes' condition."""
    if not route.is_ready:
        return False
    if not any(channel.kind is RouteChannelKind.ONLINE for channel in route.channels):
        return False
    if not consent_granted:
        return False
    return True


def _can_compare(collection_checked: bool, route_checked: bool, eligible_routes) -> bool:
    """Mirror ReviewConsent `canCompare`: BOTH consent checkboxes AND at least
    one eligible route. Consent is never auto-granted or bypassed."""
    return bool(collection_checked and route_checked and len(eligible_routes) > 0)


# ---------------------------------------------------------------------------
# VIN is OPTIONAL during general intake
# ---------------------------------------------------------------------------


def test_vin_blank_passes_base_intake_validation() -> None:
    identity = VehicleIdentity(vin=None, model_year=2022, make="TestMake", model="TestModel")
    assert identity.vin is None
    # Blank string normalizes to None too; VIN support is fully retained.
    assert VehicleIdentity(vin="", model_year=2022, make="A", model="B").vin is None
    assert VehicleIdentity(vin=SYNTHETIC_VIN, model_year=2022, make="A", model="B").vin == SYNTHETIC_VIN
    with pytest.raises(Exception):
        VehicleIdentity(vin="SHORT", model_year=2022, make="A", model="B")


def test_vin_blank_materializes_vehicle_and_is_not_globally_missing(tmp_path) -> None:
    engine, planner = make_integration_env(
        tmp_path,
        [entry("no-vin-route", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["no-vin-route"])],
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    seed_profile(engine, sid)
    engine.submit_answer(sid, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(sid, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(sid, "product_data.drivers[0].licence.expiry_date", "2030-12-31")
    # Vehicle WITHOUT a VIN: only year/make/model are supplied.
    for path, value in (
        ("product_data.vehicles[0].identity.model_year", 2022),
        ("product_data.vehicles[0].identity.make", "TestMake"),
        ("product_data.vehicles[0].identity.model", "TestModel"),
    ):
        result = engine.submit_answer(sid, path, value)
        assert result.validation_success, (path, result.error_message)
    presence = engine.field_presence(sid, [VIN, "product_data.vehicles[0].identity.make"])
    assert presence[VIN] is False
    assert presence["product_data.vehicles[0].identity.make"] is True
    # A route that does NOT require VIN is not blocked by VIN -> ready once the
    # user grants consent.
    engine.grant_route_consent(sid, "no-vin-route", [], True)
    route = next(r for r in planner.plan(sid).routes if r.registry_id == "no-vin-route")
    assert route.is_ready is True
    assert not any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == VIN for b in route.blockers
    )


# ---------------------------------------------------------------------------
# Per-route VIN requirements are data-driven (never global / hard-coded)
# ---------------------------------------------------------------------------


def test_route_requiring_vin_blocked_when_vin_absent(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("needs-vin", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["needs-vin"])],
        per_route_reqs={"needs-vin": [VIN]},
        profile_source=StubProfileSource(
            presence={LEGAL: True, POSTAL: True, LICENCE: True, VIN: False},
            consent={"needs-vin": True},
        ),
    )
    route = next(r for r in planner.plan("s").routes if r.registry_id == "needs-vin")
    assert route.is_ready is False
    assert any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == VIN for b in route.blockers
    )


def test_route_without_vin_requirement_stays_eligible_without_vin(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("no-vin", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["no-vin"])],
        profile_source=StubProfileSource(
            presence={LEGAL: True, POSTAL: True, LICENCE: True, VIN: False},
            consent={"no-vin": True},
        ),
    )
    route = next(r for r in planner.plan("s").routes if r.registry_id == "no-vin")
    assert route.is_ready is True
    assert not any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == VIN for b in route.blockers
    )


def test_vin_requirement_add_remove_changes_behavior_without_code(tmp_path) -> None:
    base = dict(
        entries=[entry("route-a", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["route-a"])],
        profile_source=StubProfileSource(
            presence={LEGAL: True, POSTAL: True, LICENCE: True, VIN: False},
            consent={"route-a": True},
        ),
    )
    # Without a VIN requirement the route is ready (VIN absent).
    without = make_planner(tmp_path, base["entries"], rate_sources=base["rate_sources"],
                           profile_source=base["profile_source"])
    route = next(r for r in without.plan("s").routes if r.registry_id == "route-a")
    assert route.is_ready is True
    # Adding VIN to per-route data flips it to blocked - a pure data change.
    with_vin = make_planner(tmp_path, base["entries"], rate_sources=base["rate_sources"],
                            per_route_reqs={"route-a": [VIN]}, profile_source=base["profile_source"])
    route = next(r for r in with_vin.plan("s").routes if r.registry_id == "route-a")
    assert route.is_ready is False
    assert any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == VIN for b in route.blockers
    )


# ---------------------------------------------------------------------------
# Consent -> readiness -> Compare (Square One / Zurich)
# ---------------------------------------------------------------------------


def test_consent_absent_square_one_blocked_and_compare_disabled(tmp_path) -> None:
    planner = _square_one_planner(tmp_path, consent={})
    route = _square_one_route(planner.plan("s"))
    # Route is online + blocked ONLY by route-disclosure consent: it appears in
    # the eligible list so the user can grant consent on screen, but Compare is
    # disabled until BOTH consent checkboxes are checked (never bypassed).
    assert route.is_ready is False
    assert any(b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in route.blockers)
    assert _frontend_eligible(route) is True
    # Compare button: disabled without both consents, enabled with both.
    assert _can_compare(False, False, [route]) is False
    assert _can_compare(True, False, [route]) is False
    assert _can_compare(False, True, [route]) is False
    assert _can_compare(True, True, [route]) is True
    # The run itself refuses to execute without consent.
    assert _run_would_execute(route, consent_granted=False) is False


def test_both_consents_granted_square_one_ready_and_compare_enabled(tmp_path) -> None:
    planner = _square_one_planner(tmp_path, consent={"square-one": True})
    route = _square_one_route(planner.plan("s"))
    # With all fields present + route-disclosure consent, Square One is ready
    # with an online channel -> the comparison run executes it (Compare on).
    assert route.is_ready is True
    assert any(channel.kind is RouteChannelKind.ONLINE for channel in route.channels)
    assert _frontend_eligible(route) is True
    assert _run_would_execute(route, consent_granted=True) is True


def test_consent_revoked_square_one_blocked_and_compare_disabled(tmp_path) -> None:
    engine, planner = make_integration_env(
        tmp_path,
        [_square_one_entry()],
        rate_sources=[rate_source("RS-ZURICH-AUTO", related_registry_ids=["square-one"])],
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    seed_profile(engine, sid)
    engine.submit_answer(sid, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(sid, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(sid, "product_data.drivers[0].licence.expiry_date", "2030-12-31")
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model_year", 2022)
    engine.submit_answer(sid, "product_data.vehicles[0].identity.make", "TestMake")
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model", "TestModel")
    engine.submit_answer(sid, VIN, SYNTHETIC_VIN)
    engine.record_collection_consent(sid)

    before = _square_one_route(planner.plan(sid))
    assert before.is_ready is False
    assert any(b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in before.blockers)
    assert _run_would_execute(before, consent_granted=False) is False

    decision = engine.grant_route_consent(sid, "square-one", [], True)
    granted = _square_one_route(planner.plan(sid))
    assert granted.is_ready is True
    assert _run_would_execute(granted, consent_granted=True) is True

    # Revoking the route-disclosure receipt re-blocks the route and disables
    # Compare - no silent re-grant.
    engine._consent.revoke(decision.consent_id)
    revoked = _square_one_route(planner.plan(sid))
    assert revoked.is_ready is False
    assert any(b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in revoked.blockers)
    assert _run_would_execute(revoked, consent_granted=False) is False


# ---------------------------------------------------------------------------
# MOCK comparison is isolated from LIVE configuration
# ---------------------------------------------------------------------------


def test_live_route_unavailable_does_not_disable_mock_comparison(tmp_path, mock_site) -> None:
    from demo_overlay_helpers import MOCK_PRIMARY, make_demo_env

    runtime, session_id = make_demo_env(tmp_path, mock_site, grant_consent=True)
    plan = runtime.planner.plan(session_id)
    # The mock plan comes from the ISOLATED demo overlay, never the live
    # registry: no Square One, and at least one mock route is ready + online +
    # consented (i.e. mock Compare stays enabled even if a LIVE route is not
    # configured/available).
    assert all(r.registry_id != "square-one" for r in plan.routes)
    mock_ids = {r.registry_id for r in plan.routes}
    assert MOCK_PRIMARY in mock_ids
    ready = [r for r in plan.routes if r.is_ready]
    assert any(_run_would_execute(r, consent_granted=True) for r in ready)


# ---------------------------------------------------------------------------
# Privacy: no applicant values in plans / URLs
# ---------------------------------------------------------------------------


def test_plan_contains_no_applicant_values(tmp_path) -> None:
    planner = _square_one_planner(tmp_path, consent={"square-one": True})
    plan = planner.plan("s")
    blob = json.dumps(plan.model_dump(mode="json"))
    for marker in SENSITIVE_MARKERS:
        assert marker not in blob
    # Public channel values are provider URLs only - never applicant data.
    for route in plan.routes:
        for channel in route.channels:
            if channel.value:
                assert channel.value.startswith("https://")


# ---------------------------------------------------------------------------
# Controlled Square One LIVE route: the LiveExecutionGate.
#
# The comparison-run service threads an explicit applicant attestation to
# manager.create(live_gate=...). Without it (the default) the browser session
# gate refuses with LIVE_GATE_REQUIRED - a safety default, never a bypass.
# These tests are HERMETIC: no real browser is ever launched and no live
# submission happens (a stub manager returns a refusal; the gate decision is
# tested directly at the validation layer).
# ---------------------------------------------------------------------------


def _verified_square_one_env(tmp_path, mock_site):
    """Hermetic browser env with a verified Square One-style route + config."""
    from app.browser.mock_site import build_scenario_config
    from browser_helpers import make_browser_env

    return make_browser_env(
        tmp_path,
        mock_site,
        registry_id="square-one",
        entry_overrides={
            "status": "verified",
            "quote_url": "https://www.squareone.ca/auto-insurance/",
            "insurer_group": "Zurich",
            "requirements": ["licence", "vin"],
            "last_verified_at": "2026-08-12T18:09:43.298460+00:00",
        },
        route_config=build_scenario_config("square-one", mock_site, "quote"),
    )


def test_square_one_live_gate_satisfied_passes_hermetic(tmp_path, mock_site) -> None:
    from app.models.browser.session import BrowserRefusalReason, LiveExecutionGate

    env = _verified_square_one_env(tmp_path, mock_site)
    entry = env.registry.get_by_registry_id("square-one")
    assert entry is not None and entry.status.value == "verified"
    now = dt.datetime.now(dt.timezone.utc)

    # No gate -> the exact gate currently failing: LIVE_GATE_REQUIRED.
    refusal = env.manager._validate_live(
        env.session_id, "square-one", "square-one", entry, None, now
    )
    assert refusal is not None
    assert refusal.reason is BrowserRefusalReason.LIVE_GATE_REQUIRED

    # A half-satisfied gate is still refused (never auto-granted).
    partial = LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=False)
    refusal = env.manager._validate_live(
        env.session_id, "square-one", "square-one", entry, partial, now
    )
    assert refusal is not None
    assert refusal.reason is BrowserRefusalReason.LIVE_GATE_REQUIRED

    # A satisfied gate passes EVERY live check (verified + quote_url + config).
    # We test the decision only - no browser is launched.
    gate = LiveExecutionGate(
        personal_use_confirmed=True, accurate_information_attested=True, attested_at=now
    )
    assert env.manager._validate_live(
        env.session_id, "square-one", "square-one", entry, gate, now
    ) is None


class _CapturingLiveManager:
    """Stub browser manager that records the live_gate and NEVER launches.

    Always returns a refusal so the run service short-circuits before any
    browser workflow / network activity (hermetic).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        gate = kwargs.get("live_gate")
        satisfied = gate is not None and gate.satisfied
        return BrowserStartRefusal(
            intake_session_id=kwargs["intake_session_id"],
            planned_route_id=kwargs["planned_route_id"],
            registry_id=kwargs["planned_route_id"],
            reason=(
                BrowserRefusalReason.LIVE_GATE_REQUIRED
                if not satisfied
                else BrowserRefusalReason.ROUTE_NOT_READY
            ),
            detail="stub: gate satisfied - would start live browser" if satisfied
            else "stub: live gate missing or unsatisfied",
            refused_at=dt.datetime.now(dt.timezone.utc),
        )

    @property
    def last_gate(self):
        return self.calls[-1]["live_gate"] if self.calls else None


def _live_run_service(env, stub):
    from app.services.comparison_run import ComparisonRunService

    return ComparisonRunService(
        planner=env.planner,
        manager=stub,
        recovery=env.recovery,
        intake=env.engine,
        evidence=env.evidence,
        normalization=env.normalization,
        comparison=env.comparison,
    )


async def test_comparison_run_live_gate_threads_to_manager(tmp_path, mock_site) -> None:
    from app.models.browser.session import LiveExecutionGate
    from comparison_run_helpers import await_run, make_comparison_run_env

    env = make_comparison_run_env(tmp_path, mock_site)
    stub = _CapturingLiveManager()
    service = _live_run_service(env, stub)
    env.run_service = service

    gate = LiveExecutionGate(
        personal_use_confirmed=True,
        accurate_information_attested=True,
        attested_at=dt.datetime.now(dt.timezone.utc),
    )
    run = service.start_run(env.session_id, "live", live_gate=gate)
    final = await await_run(env, run.comparison_run_id)

    assert stub.calls, "live manager.create should have been invoked"
    assert stub.last_gate is gate
    assert stub.last_gate.satisfied is True
    # Because the gate was threaded, the refusal is NOT live_gate_required.
    assert all("live_gate_required" not in (r.message or "") for r in final.route_summaries)


async def test_comparison_run_without_live_gate_still_refused(tmp_path, mock_site) -> None:
    from comparison_run_helpers import await_run, make_comparison_run_env

    env = make_comparison_run_env(tmp_path, mock_site)
    stub = _CapturingLiveManager()
    service = _live_run_service(env, stub)
    env.run_service = service

    run = service.start_run(env.session_id, "live")  # NO live gate (default)
    final = await await_run(env, run.comparison_run_id)

    assert stub.calls, "live manager.create should have been invoked"
    assert stub.last_gate is None
    # Safe default: the route is refused with LIVE_GATE_REQUIRED, never bypassed.
    assert any("live_gate_required" in (r.message or "") for r in final.route_summaries)


def test_start_comparison_run_request_accepts_live_gate() -> None:
    from app.api.comparison_runs import StartComparisonRunRequest
    from app.models.browser.session import LiveExecutionGate

    req = StartComparisonRunRequest(
        intake_session_id="s1",
        execution_mode="live",
        live_gate=LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=True),
    )
    assert req.live_gate is not None and req.live_gate.satisfied is True
    # Default: no gate -> None (safe).
    assert StartComparisonRunRequest(intake_session_id="s1", execution_mode="live").live_gate is None
    # extra="forbid" preserved.
    with pytest.raises(Exception):
        StartComparisonRunRequest(intake_session_id="s1", execution_mode="live", bogus=1)


# ---------------------------------------------------------------------------
# Ontario licence format + progressive intake / JIT missing-field collection
# ---------------------------------------------------------------------------

ANNUAL_KM = "product_data.vehicles[0].use.annual_kilometres"


def _driver_identity(licence: str):
    from app.models.insurance.auto.driver import LicenceIdentity

    return LicenceIdentity(
        name_on_licence=SYNTHETIC_LEGAL_NAME,
        licence_number=licence,
        expiry_date=dt.date(2030, 12, 31),
    )


def test_licence_correct_format_accepted() -> None:
    # Canonical Ontario format L####-#####-##### is accepted and normalized.
    lic = _driver_identity("A1234-56789-01234")
    assert lic.licence_number == "A1234-56789-01234"
    # The shared synthetic fixture now uses the correct format too.
    assert SYNTHETIC_LICENCE == "T0000-00000-00000"
    assert _driver_identity(SYNTHETIC_LICENCE).licence_number == SYNTHETIC_LICENCE


def test_licence_old_fixture_format_rejected(tmp_path) -> None:
    # The old fixture format is rejected by the schema...
    with pytest.raises(Exception):
        _driver_identity("T0000-0000000-0000")
    # ...and by the intake engine, with the expected format in the message.
    from intake_helpers import make_engine

    engine = make_engine(tmp_path)
    session, _ = engine.create_session(InsuranceType.AUTO)
    seed_profile(engine, session.session_id)
    result = engine.submit_answer(session.session_id, LICENCE, "T0000-0000000-0000")
    assert result.validation_success is False
    assert "A1234-56789-01234" in result.error_message


def test_licence_lowercase_whitespace_normalizes() -> None:
    from factories import make_insurance_profile

    profile = make_insurance_profile()
    updated = profile.updated(LICENCE, "  t0000-00000-00000  ")
    assert updated.product_data.drivers[0].licence.licence_number == "T0000-00000-00000"


def test_partial_intake_can_reach_review(tmp_path) -> None:
    # A partial profile (driver only - no vehicle, no VIN, no annual_km) can be
    # planned and reach Review: routes not requiring the missing fields stay
    # eligible, and nothing is fabricated.
    engine, planner = make_integration_env(
        tmp_path,
        [entry("partial-ok", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["partial-ok"])],
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    seed_profile(engine, sid)
    engine.submit_answer(sid, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(sid, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(sid, "product_data.drivers[0].licence.expiry_date", "2030-12-31")
    engine.grant_route_consent(sid, "partial-ok", [], True)
    plan = planner.plan(sid)  # planning succeeds on a partial profile
    route = next(r for r in plan.routes if r.registry_id == "partial-ok")
    assert route.is_ready is True
    # VIN is genuinely absent - never fabricated.
    assert engine.field_presence(sid, [VIN])[VIN] is False


def test_eligible_provider_runs_despite_unrelated_missing(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [
            entry("ready-a", distinct_rate_source_id="RS-1"),
            entry("needs-x", distinct_rate_source_id="RS-2"),
        ],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["ready-a"]),
            rate_source("RS-2", related_registry_ids=["needs-x"]),
        ],
        per_route_reqs={"needs-x": [ANNUAL_KM]},
        profile_source=StubProfileSource(
            presence={LEGAL: True, POSTAL: True, LICENCE: True, ANNUAL_KM: False},
            consent={"ready-a": True, "needs-x": True},
        ),
    )
    by_id = {r.registry_id: r for r in planner.plan("s").routes}
    assert by_id["ready-a"].is_ready is True
    assert by_id["needs-x"].is_ready is False


def test_missing_field_blocks_only_that_provider(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [
            entry("ready-a", distinct_rate_source_id="RS-1"),
            entry("needs-x", distinct_rate_source_id="RS-2"),
        ],
        rate_sources=[
            rate_source("RS-1", related_registry_ids=["ready-a"]),
            rate_source("RS-2", related_registry_ids=["needs-x"]),
        ],
        per_route_reqs={"needs-x": [ANNUAL_KM]},
        profile_source=StubProfileSource(
            presence={LEGAL: True, POSTAL: True, LICENCE: True, ANNUAL_KM: False},
            consent={"ready-a": True, "needs-x": True},
        ),
    )
    by_id = {r.registry_id: r for r in planner.plan("s").routes}
    blocked = {
        b.canonical_path
        for b in by_id["needs-x"].blockers
        if b.kind is RouteBlockerKind.MISSING_FIELD
    }
    assert blocked == {ANNUAL_KM}
    assert not any(
        b.kind is RouteBlockerKind.MISSING_FIELD for b in by_id["ready-a"].blockers
    )


def test_missing_field_action_returns_exact_provider_gaps(tmp_path) -> None:
    # Backend contract behind the "Complete required information" action (which
    # returns to the preserved form): the JIT missing-field surface reports
    # exactly what the blocked provider needs.
    engine, planner = make_integration_env(
        tmp_path,
        [entry("needs-x", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["needs-x"])],
        per_route_reqs={"needs-x": [ANNUAL_KM]},
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    seed_profile(engine, sid)
    engine.submit_answer(sid, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(sid, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(sid, "product_data.drivers[0].licence.expiry_date", "2030-12-31")
    outcomes = planner.request_missing_fields(sid)
    assert any(o.canonical_path == ANNUAL_KM for o in outcomes)


def test_filling_missing_value_makes_route_eligible(tmp_path) -> None:
    engine, planner = make_integration_env(
        tmp_path,
        [entry("needs-x", distinct_rate_source_id="RS-1")],
        rate_sources=[rate_source("RS-1", related_registry_ids=["needs-x"])],
        per_route_reqs={"needs-x": [ANNUAL_KM]},
    )
    session, _ = engine.create_session(InsuranceType.AUTO)
    sid = session.session_id
    seed_profile(engine, sid)
    engine.submit_answer(sid, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(sid, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(sid, "product_data.drivers[0].licence.expiry_date", "2030-12-31")
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model_year", 2022)
    engine.submit_answer(sid, "product_data.vehicles[0].identity.make", "TestMake")
    engine.submit_answer(sid, "product_data.vehicles[0].identity.model", "TestModel")
    engine.grant_route_consent(sid, "needs-x", [], True)
    before = next(r for r in planner.plan(sid).routes if r.registry_id == "needs-x")
    assert before.is_ready is False
    assert any(
        b.kind is RouteBlockerKind.MISSING_FIELD and b.canonical_path == ANNUAL_KM
        for b in before.blockers
    )
    engine.submit_answer(sid, ANNUAL_KM, 12000)
    after = next(r for r in planner.plan(sid).routes if r.registry_id == "needs-x")
    assert after.is_ready is True


def test_consent_and_live_gates_still_enforced(tmp_path, mock_site) -> None:
    # Route-disclosure consent remains mandatory (no bypass).
    planner = _square_one_planner(tmp_path, consent={})
    route = _square_one_route(planner.plan("s"))
    assert any(b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in route.blockers)
    # Live attestation gate remains mandatory (no bypass).
    env = _verified_square_one_env(tmp_path, mock_site)
    entry_obj = env.registry.get_by_registry_id("square-one")
    refusal = env.manager._validate_live(
        env.session_id, "square-one", "square-one", entry_obj, None, dt.datetime.now(dt.timezone.utc)
    )
    assert refusal is not None
    assert refusal.reason is BrowserRefusalReason.LIVE_GATE_REQUIRED


def test_sensitive_values_never_leak(tmp_path) -> None:
    from app.core.redaction import redact_text

    # The synthetic licence never appears in plans (paths only) or redacted text.
    planner = _square_one_planner(tmp_path, consent={"square-one": True})
    blob = json.dumps(planner.plan("s").model_dump(mode="json"))
    assert SYNTHETIC_LICENCE not in blob
    for marker in SENSITIVE_MARKERS:
        assert marker not in blob
    redacted = redact_text(f"licence {SYNTHETIC_LICENCE}")
    assert SYNTHETIC_LICENCE not in redacted
