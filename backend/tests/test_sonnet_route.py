"""Sonnet onboarding as a bounded controlled LIVE route - hermetic tests.

Covers the data-driven Sonnet onboard (registry ``RS-SONNET-AUTO`` mapping,
rate-source record, and the complete bounded browser route config) and the
behavior the live route must guarantee:

- registry verified + distinct rate source mapped ONLY to sonnet (never
  conflated with Square One / RS-ZURICH-AUTO);
- browser config is valid, bounded to ``secure.sonnet.ca``, has quote /
  callback / access-control / validation detection, and contains no applicant
  values;
- consent + live attestation gates are preserved (LIVE_GATE_REQUIRED default);
- bounded execution STOPS at CAPTCHA / access restriction (no solve/bypass),
  preserves a redacted callback blocker with NO fabricated quote, and stops at
  human checkpoints before purchase/declaration (requires_human,
  must_not_automate);
- a returned premium is extracted ONLY when explicitly present (never
  invented).

All tests are hermetic (local mock quote site; no real Sonnet, no LLM, no
LangSmith). The real Sonnet site is only ever driven by the participant in a
controlled, headed, human-approved live run.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.browser.config import BrowserRouteConfigLoader
from app.browser.mock_site import mock_scenario_url
from app.browser.session import BrowserExecutionMode
from app.models.browser.session import BrowserRefusalReason, LiveExecutionGate
from app.models.route_planner import RouteBlockerKind
from app.services.deduplication import RateSourceDeduplicationService
from app.services.market_registry import MarketRegistryService

from browser_helpers import make_browser_env

REAL_BACKEND = Path(__file__).resolve().parents[1]
REAL_ROUTES_DIR = REAL_BACKEND / "data" / "browser" / "routes"
REAL_REGISTRY_DIR = REAL_BACKEND / "data" / "market_registry"
REAL_RATE_SOURCES_DIR = REAL_BACKEND / "data" / "rate_sources"

# Applicant markers that must NEVER appear in configs, plans, observations,
# workflow state, or evidence (synthetic fixtures are fine in test code, not in
# production data files / live artifacts).
SENSITIVE = [
    "T0000-00000-00000",        # synthetic licence
    "1HGCM82633A000000",        # synthetic VIN
    "1990-01-01",               # synthetic DOB
    "123 Test Street",          # synthetic street
    "Test Applicant",           # synthetic legal name
    "applicant.test@example.com",  # synthetic email
    "M0A 0A0",                  # synthetic postal
    "M5V 1A1",                  # applicant postal
]

SONNET_QUOTE_URL = "https://secure.sonnet.ca/#/quoting/auto/province?lang=en"


def _sonnet_scenario_config(site, scenario):
    """The REAL sonnet.json config, overlaid onto a local mock scenario page."""
    cfg = BrowserRouteConfigLoader(config_dir=REAL_ROUTES_DIR).load("sonnet")
    # The executor's _host_allowed compares the URL's HOSTNAME (no port).
    host = urlsplit(site.url("/")).hostname
    return cfg.model_copy(
        update={
            "start_url": mock_scenario_url(site, scenario),
            "allowed_hosts": [host],
        }
    )


def _sonnet_env(tmp_path, mock_site, scenario, *, verified: bool = False, grant_consent: bool = True):
    overrides = {"quote_url": mock_scenario_url(mock_site, scenario)}
    if verified:
        overrides.update({"status": "verified", "last_verified_at": "2026-08-12T00:00:00+00:00"})
    return make_browser_env(
        tmp_path,
        mock_site,
        registry_id="sonnet",
        entry_overrides=overrides,
        route_config=_sonnet_scenario_config(mock_site, scenario),
        grant_consent=grant_consent,
    )


async def _run(env, session_id: str, entry: str = "run", max_steps: int = 20) -> dict:
    from app.graph.browser_workflow import build_browser_workflow

    return await build_browser_workflow(env.manager).ainvoke(
        {"entry": entry, "browser_session_id": session_id, "max_steps": max_steps}
    )


async def _stop(env, session_id: str) -> None:
    try:
        await env.manager.close(session_id)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Registry + distinct rate source (RS-SONNET-AUTO)
# ---------------------------------------------------------------------------


def test_sonnet_registry_mapped_to_rs_sonnet_auto() -> None:
    registry = MarketRegistryService(registry_dir=REAL_REGISTRY_DIR)
    sonnet = registry.get_by_registry_id("sonnet")
    assert sonnet is not None
    assert sonnet.status.value == "verified"
    assert sonnet.distinct_rate_source_id == "RS-SONNET-AUTO"
    assert sonnet.legal_underwriter == "Sonnet Insurance Company"
    assert sonnet.quote_url == SONNET_QUOTE_URL
    assert sonnet.active is True

    dedup = RateSourceDeduplicationService(
        registry_service=registry, rate_sources_dir=REAL_RATE_SOURCES_DIR
    )
    rs = dedup.get_rate_source("RS-SONNET-AUTO")
    assert rs is not None
    assert rs.related_registry_ids == ["sonnet"]
    assert "Sonnet Insurance Company" in rs.legal_underwriters


def test_sonnet_rate_source_never_conflated_with_square_one() -> None:
    registry = MarketRegistryService(registry_dir=REAL_REGISTRY_DIR)
    dedup = RateSourceDeduplicationService(
        registry_service=registry, rate_sources_dir=REAL_RATE_SOURCES_DIR
    )
    # Sonnet maps to its OWN rate source; Square One stays on Zurich.
    assert dedup.get_rate_source("RS-SONNET-AUTO").related_registry_ids == ["sonnet"]
    assert dedup.get_rate_source("RS-ZURICH-AUTO").related_registry_ids == ["square-one"]
    assert registry.get_by_registry_id("square-one").distinct_rate_source_id == "RS-ZURICH-AUTO"
    ids = {r.distinct_rate_source_id for r in dedup.list_rate_sources()}
    assert {"RS-SONNET-AUTO", "RS-ZURICH-AUTO"} <= ids


# ---------------------------------------------------------------------------
# Browser route config: valid, bounded, no applicant values
# ---------------------------------------------------------------------------


def test_sonnet_route_config_valid_bounded_no_pii() -> None:
    loader = BrowserRouteConfigLoader(config_dir=REAL_ROUTES_DIR)
    cfg = loader.load("sonnet")
    assert cfg.registry_id == "sonnet"
    assert cfg.start_url == SONNET_QUOTE_URL
    assert cfg.allowed_hosts == ["secure.sonnet.ca"]
    # Complete detection blocks so the route is BOUNDED.
    assert cfg.quote_detection.heading_patterns
    assert cfg.quote_detection.premium_label_patterns
    assert cfg.callback_detection.patterns
    assert cfg.access_control_detection.patterns
    assert cfg.access_control_detection.iframe_src_patterns
    assert cfg.validation_detection.patterns
    # The two vehicle/driver count bindings are derived (collection_length) and
    # non-sensitive; they are the only derived-count bindings.
    count_ids = {
        "ss-auto-interstitial-vehicles-input",
        "ss-auto-interstitial-drivers-input",
    }
    counts = [b for b in cfg.field_bindings if b.external_field_id in count_ids]
    assert len(counts) == 2
    for binding in counts:
        assert binding.transform.value == "collection_length"
        assert binding.sensitivity.value == "non_sensitive"

    # The autonomous-first v5 config maps canonical fields; every applicant-
    # identifying canonical path must be declared SENSITIVE so its value is
    # never logged/traced/emitted as evidence.
    sensitive_paths = {
        "product_data.vehicles[0].identity.vin",
        "applicant.identity.date_of_birth",
        "applicant.address.postal_code",
        "applicant.identity.legal_name",
        "product_data.drivers[0].licence.licence_number",
        "product_data.drivers[0].licence.name_on_licence",
        "product_data.drivers[0].licence.expiry_date",
    }
    marked_sensitive = {
        b.canonical_path for b in cfg.field_bindings if b.sensitivity.value == "sensitive"
    }
    assert sensitive_paths <= marked_sensitive

    # The serialized config + raw file carry no applicant values.
    blob = json.dumps(cfg.model_dump(mode="json"))
    raw = (REAL_ROUTES_DIR / "sonnet.json").read_text(encoding="utf-8")
    for marker in SENSITIVE:
        assert marker not in blob
        assert marker not in raw


# ---------------------------------------------------------------------------
# Consent + live attestation gates are preserved
# ---------------------------------------------------------------------------


async def test_sonnet_live_gate_still_enforced(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, "applicant", verified=True)
    entry = env.registry.get_by_registry_id("sonnet")
    now = dt.datetime.now(dt.timezone.utc)

    # No gate (the default) -> refused with LIVE_GATE_REQUIRED.
    refusal = env.manager._validate_live(env.session_id, "sonnet", "sonnet", entry, None, now)
    assert refusal is not None
    assert refusal.reason is BrowserRefusalReason.LIVE_GATE_REQUIRED

    # A half-satisfied gate is still refused (never auto-granted).
    partial = LiveExecutionGate(personal_use_confirmed=True, accurate_information_attested=False)
    refusal = env.manager._validate_live(env.session_id, "sonnet", "sonnet", entry, partial, now)
    assert refusal is not None
    assert refusal.reason is BrowserRefusalReason.LIVE_GATE_REQUIRED

    # Satisfied gate passes live validation (decision only - no browser).
    gate = LiveExecutionGate(
        personal_use_confirmed=True,
        accurate_information_attested=True,
        attested_at=now,
    )
    assert env.manager._validate_live(env.session_id, "sonnet", "sonnet", entry, gate, now) is None


async def test_sonnet_route_disclosure_consent_gate(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, "applicant", grant_consent=False)
    plan = env.planner.plan(env.session_id)
    route = next(r for r in plan.routes if r.registry_id == "sonnet")
    assert route.is_ready is False
    assert any(b.kind is RouteBlockerKind.CONSENT_REQUIRED for b in route.blockers)

    env.engine.grant_route_consent(env.session_id, "sonnet", [], True)
    after = next(r for r in env.planner.plan(env.session_id).routes if r.registry_id == "sonnet")
    assert after.is_ready is True


# ---------------------------------------------------------------------------
# Bounded execution against the local mock site (real sonnet detection config)
# ---------------------------------------------------------------------------


async def test_sonnet_bounded_captcha_stops_no_bypass_no_pii(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, "captcha")
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "stopped_access_control"
        assert state["observation_type"] == "access_control_detected"
        result = env.manager.last_result(bs.browser_session_id)
        assert result.observation.quote is None  # no quote fabricated
        payload = str(state) + result.model_dump_json()
        for marker in SENSITIVE:
            assert marker not in payload
    finally:
        await _stop(env, bs.browser_session_id)


async def test_sonnet_bounded_callback_redacted_blocker_no_quote(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, "callback")
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        result = env.manager.last_result(bs.browser_session_id)
        assert result.observation.observation_type.value == "callback_detected"
        assert state["observation_type"] == "callback_detected"
        # No quote was returned - nothing fabricated or inferred.
        assert result.observation.quote is None
        assert state.get("quote_present") is not True
        payload = str(state) + result.model_dump_json()
        for marker in SENSITIVE:
            assert marker not in payload
    finally:
        await _stop(env, bs.browser_session_id)


@pytest.mark.parametrize(
    "scenario,expected_status,must_not_automate",
    [
        # Application declaration: terminal PROHIBITED stop (never automated,
        # never approvable). The applicant must accept it personally.
        ("declaration", "stopped_prohibited", True),
        # Purchase boundary: hard STOP, never automated.
        ("payment", "stopped_prohibited", True),
    ],
)
async def test_sonnet_human_checkpoint_before_purchase_stops(
    scenario: str, expected_status: str, must_not_automate: bool, tmp_path, mock_site
) -> None:
    env = _sonnet_env(tmp_path, mock_site, scenario)
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == expected_status
        result = env.manager.last_result(bs.browser_session_id)
        checkpoint = result.observation.checkpoint
        assert checkpoint is not None
        # A human is ALWAYS required at the boundary; declaration and purchase
        # are both terminal prohibited actions that automation never performs.
        assert checkpoint.requires_human is True
        assert checkpoint.must_not_automate is must_not_automate
        payload = str(state) + result.model_dump_json()
        for marker in SENSITIVE:
            assert marker not in payload
    finally:
        await _stop(env, bs.browser_session_id)


async def test_sonnet_quote_extracted_only_when_explicit(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, "quote-annual")
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        state = await _run(env, bs.browser_session_id)
        assert state["workflow_status"] == "succeeded"
        assert state["observation_type"] == "quote_detected"
        raw = env.manager.last_result(bs.browser_session_id).observation.quote.raw
        # The premium is extracted only because the page explicitly shows it.
        assert raw.annual_amount_parsed == 1200.0
        assert raw.currency == "CAD"
        # No applicant values leak into the observation.
        payload = env.manager.last_result(bs.browser_session_id).model_dump_json()
        for marker in SENSITIVE:
            assert marker not in payload
    finally:
        await _stop(env, bs.browser_session_id)
