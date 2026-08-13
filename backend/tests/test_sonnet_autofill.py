"""Sonnet autonomous-first autofill - hermetic proof tests.

Proves the v4 ``sonnet.json`` mapping against a Sonnet-shaped local mock screen:

- Province=Ontario is selected AUTOMATICALLY from the non-PII route constant;
- MORE than just the two count fields are auto-filled (canonical JIT values);
- values never appear in action events, structured logs, or redacted evidence;
- unknown Sonnet questions -> resumable human fallback (browser stays open,
  same browser_session_id, no restart);
- CAPTCHA / declaration / payment are never automated;
- an explicitly displayed premium is extracted.

Hermetic: local mock quote site only; no real Sonnet, no LLM, no LangSmith.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.browser.config import BrowserRouteConfigLoader
from app.browser.mock_site import mock_scenario_url
from app.browser.session import BrowserExecutionMode
from app.models.evidence import EvidenceEventType

from browser_helpers import make_browser_env
from evidence_helpers import (
    SENSITIVE_MARKERS,
    assert_evidence_privacy_safe,
    make_sink_env,
)

REAL_ROUTES_DIR = Path(__file__).resolve().parents[1] / "data" / "browser" / "routes"

PROVINCE_PATH = "applicant.address.province"
VIN_PATH = "product_data.vehicles[0].identity.vin"
YEAR_PATH = "product_data.vehicles[0].identity.model_year"
MAKE_PATH = "product_data.vehicles[0].identity.make"
MODEL_PATH = "product_data.vehicles[0].identity.model"
POSTAL_PATH = "applicant.address.postal_code"
DOB_PATH = "applicant.identity.date_of_birth"
LICENCE_PATH = "product_data.drivers[0].licence.licence_number"
LIABILITY_PATH = "product_data.coverage.third_party_liability.selected_limit"


def _sonnet_config(site, scenario):
    cfg = BrowserRouteConfigLoader(config_dir=REAL_ROUTES_DIR).load("sonnet")
    host = urlsplit(site.url("/")).hostname
    return cfg.model_copy(
        update={"start_url": mock_scenario_url(site, scenario), "allowed_hosts": [host]}
    )


def _sonnet_env(tmp_path, mock_site, scenario="sonnet", *, evidence_sink=None):
    return make_browser_env(
        tmp_path,
        mock_site,
        registry_id="sonnet",
        entry_overrides={"quote_url": mock_scenario_url(mock_site, scenario)},
        route_config=_sonnet_config(mock_site, scenario),
        evidence_sink=evidence_sink,
    )


async def _stop(env, session_id) -> None:
    try:
        await env.manager.close(session_id)
    except Exception:
        pass
    try:
        await env.browser_manager.stop()
    except Exception:
        pass


async def _run_to_quote(env):
    """start (fill incl. licence) -> pause at licence-submission checkpoint ->
    approve -> step (quote page). Returns (bs, r1, r2).

    The executor MAY fill the licence field automatically, but the click that
    submits it / triggers the identity lookup MUST wait for explicit approval.
    """
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    r1 = await env.manager.start_session(bs.browser_session_id)
    assert r1.status.value == "paused_human_checkpoint"
    assert r1.observation.checkpoint is not None
    assert r1.observation.checkpoint.checkpoint_type == "identity_lookup"
    assert r1.observation.checkpoint.requires_human is True
    # No submitting click was performed before approval.
    assert not any(e.action == "click" for e in r1.action_events)
    env.manager.approve_checkpoint(bs.browser_session_id, "identity_lookup")
    # After approval: the first step submits the licence (Continue), the next
    # step observes the explicit quote result.
    r_submit = await env.manager.step_session(bs.browser_session_id)
    assert r_submit.observation_type.value == "fields_filled"
    assert r_submit.status.value == "running"
    assert any(e.action == "click" and e.status == "success" for e in r_submit.action_events)
    r_quote = await env.manager.step_session(bs.browser_session_id)
    return bs, r1, r_quote


# ---------------------------------------------------------------------------
# Province constant + more than two auto-filled fields + explicit premium
# ---------------------------------------------------------------------------


async def test_sonnet_province_constant_and_autonomous_fills(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site)
    bs, r1, r2 = await _run_to_quote(env)
    try:
        assert r2.observation_type.value == "quote_detected"
        assert r2.observation.quote.raw.annual_amount_parsed == 1200.0  # explicit premium

        events = r1.action_events + r2.action_events
        fills = [e for e in events if e.action in ("fill", "select")]
        # Province + counts + vehicle/driver/address/coverage fields -> far more
        # than only the two count fields are automatically filled.
        assert len(fills) > 2

        # Province=Ontario is selected automatically from the route constant.
        province = [e for e in fills if e.canonical_field == PROVINCE_PATH]
        assert province and province[-1].status == "success"

        fields = {e.canonical_field for e in fills}
        assert {VIN_PATH, YEAR_PATH, MAKE_PATH, MODEL_PATH, POSTAL_PATH, DOB_PATH,
                LICENCE_PATH, LIABILITY_PATH} <= fields
        assert any(e.action == "extract" and e.status == "success" for e in r2.action_events)
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Values never appear in events / logs / evidence
# ---------------------------------------------------------------------------


async def test_sonnet_values_never_in_events_logs_evidence(tmp_path, mock_site, caplog) -> None:
    ev_env, sink = make_sink_env()
    caplog.set_level(logging.INFO, logger="app.browser.executor")
    env = _sonnet_env(tmp_path, mock_site, evidence_sink=sink)
    bs, r1, r2 = await _run_to_quote(env)
    try:
        events = r1.action_events + r2.action_events
        assert events
        blob = "\n".join(e.model_dump_json() for e in events)
        for marker in SENSITIVE_MARKERS:
            assert marker not in blob

        for record in caplog.records:
            if record.getMessage().startswith("browser_action"):
                for marker in SENSITIVE_MARKERS:
                    assert marker not in record.getMessage()

        records = await ev_env.repo.list_by_intake(env.session_id)
        field_events = [
            r for r in records if r.event_type is EvidenceEventType.FIELD_INTERACTION_OBSERVED
        ]
        assert any(r.payload.canonical_path == VIN_PATH for r in field_events)
        assert_evidence_privacy_safe(records)
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Unknown question -> resumable human fallback (browser stays open)
# ---------------------------------------------------------------------------


async def test_sonnet_unknown_field_resumable_fallback(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, scenario="unknown")
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        assert result.observation_type.value == "unknown_external_field"
        assert result.status.value == "paused_unknown_field"
        # The browser session stays open for human fallback (same id, not closed).
        session = env.manager.get(bs.browser_session_id)
        assert session.browser_session_id == bs.browser_session_id
        assert session.status.value == "paused_unknown_field"
        # Resume re-inspects the SAME page/session (no restart).
        r2 = await env.manager.step_session(bs.browser_session_id)
        assert r2 is not None
        assert r2.status.value == "paused_unknown_field"
        assert env.manager.get(bs.browser_session_id).browser_session_id == bs.browser_session_id
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# CAPTCHA / declaration / payment are never automated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,expected_status,expected_must_not_automate",
    [
        ("captcha", "stopped_access_control", None),
        # Application declaration is a terminal PROHIBITED boundary - the
        # applicant must personally accept it; automation never may.
        ("declaration", "stopped_prohibited", True),
        ("payment", "stopped_prohibited", True),
    ],
)
async def test_sonnet_barriers_never_automated(
    scenario: str, expected_status: str, expected_must_not_automate: bool, tmp_path, mock_site
) -> None:
    env = _sonnet_env(tmp_path, mock_site, scenario=scenario)
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        assert result.status.value == expected_status
        if result.observation.checkpoint is not None:
            assert result.observation.checkpoint.requires_human is True
            if expected_must_not_automate is not None:
                assert result.observation.checkpoint.must_not_automate is expected_must_not_automate
    finally:
        await _stop(env, bs.browser_session_id)
