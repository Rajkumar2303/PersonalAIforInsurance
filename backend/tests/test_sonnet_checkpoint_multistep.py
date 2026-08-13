"""Final pre-live verification: multistep Sonnet SPA + licence-submission
checkpoint + explicit approval + same-session resume + missing-value pause.

Covers requirements 1, 2, 3, 5 and 6 of the pre-live verification:

- The real sonnet.json journey is driven as a MULTI-STEP SPA against a local
  mock: Province -> vehicle -> driver -> quote result, proving the generic
  executor loop observe -> fill -> Continue -> observe next screen -> repeat.
- On the DRIVER screen the executor fills the licence number automatically
  (allowed), but PAUSES at an identity_lookup human checkpoint BEFORE the
  click that submits the licence / triggers an identity or database lookup.
- Explicit participant approval (approve_checkpoint) then resumes the SAME
  browser_session_id + attempt_id, and only after approval is Continue clicked.
- Declaration / third-party consent are pre-fill human checkpoints.
- A missing canonical value pauses (paused_needs_field) instead of filling
  blank or clicking Continue.

Hermetic: local mock quote site only; no real Sonnet, no LLM, no LangSmith.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from app.browser.config import BrowserRouteConfigLoader
from app.browser.mock_site import mock_scenario_url
from app.browser.session import BrowserExecutionMode
from app.models.evidence import EvidenceEventType
from app.services.recovery.engine import RecoveryEngine

from browser_helpers import make_browser_env
from evidence_helpers import SENSITIVE_MARKERS, assert_evidence_privacy_safe, make_sink_env
from personas import make_standard_auto_profile

REAL_ROUTES_DIR = Path(__file__).resolve().parents[1] / "data" / "browser" / "routes"

LICENCE_PATH = "product_data.drivers[0].licence.licence_number"
ANNUAL_KM_PATH = "product_data.vehicles[0].use.annual_kilometres"


def _sonnet_config(site, scenario):
    cfg = BrowserRouteConfigLoader(config_dir=REAL_ROUTES_DIR).load("sonnet")
    host = urlsplit(site.url("/")).hostname
    return cfg.model_copy(
        update={"start_url": mock_scenario_url(site, scenario), "allowed_hosts": [host]}
    )


def _sonnet_env(tmp_path, mock_site, scenario="sonnet", *, persona=None, evidence_sink=None, recovery=None):
    return make_browser_env(
        tmp_path,
        mock_site,
        registry_id="sonnet",
        entry_overrides={"quote_url": mock_scenario_url(mock_site, scenario)},
        route_config=_sonnet_config(mock_site, scenario),
        persona=persona,
        evidence_sink=evidence_sink,
        recovery=recovery,
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


# ---------------------------------------------------------------------------
# Req 5 + req 1/2/3: multistep SPA progression + licence-submission checkpoint
# ---------------------------------------------------------------------------


async def test_sonnet_multistep_spa_progression_and_licence_checkpoint(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, scenario="sonnet-multistep", recovery=RecoveryEngine())
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        # Screen 1: Province (constant) + derived counts -> Continue.
        r1 = await env.manager.start_session(bs.browser_session_id)
        assert r1.observation_type.value == "fields_filled"
        assert r1.status.value == "running"
        assert any(e.action == "click" and e.status == "success" for e in r1.action_events)
        assert not any(e.action == "pause" for e in r1.action_events)

        # The recovery engine gives the session its OWN attempt_id on first run.
        started = env.manager.get(bs.browser_session_id)
        assert started.attempt_id is not None

        # Screen 2: vehicle identity/use/risk -> Continue (no licence on this
        # screen, so no licence checkpoint fires here).
        r2 = await env.manager.step_session(bs.browser_session_id)
        assert r2.observation_type.value == "fields_filled"
        assert r2.status.value == "running"
        vehicle_fills = [e for e in r2.action_events if e.action in ("fill", "select")]
        assert any((e.canonical_field or "").endswith("identity.vin") for e in vehicle_fills)
        assert any(e.action == "click" and e.status == "success" for e in r2.action_events)

        # Screen 3: driver screen - licence field IS filled (allowed), but the
        # submitting Continue click MUST wait for explicit approval.
        r3 = await env.manager.step_session(bs.browser_session_id)
        assert r3.status.value == "paused_human_checkpoint"
        assert r3.observation_type.value == "human_checkpoint"
        checkpoint = r3.observation.checkpoint
        assert checkpoint is not None
        assert checkpoint.checkpoint_type == "identity_lookup"
        assert checkpoint.requires_human is True
        assert checkpoint.must_not_automate is False  # resumable with approval
        fills3 = [e for e in r3.action_events if e.action in ("fill", "select")]
        assert any(e.canonical_field == LICENCE_PATH for e in fills3)  # licence filled
        # No submitting click before approval.
        assert not any(e.action == "click" for e in r3.action_events)
        # No value ever appears in any action event.
        blob = "\n".join(e.model_dump_json() for e in r3.action_events)
        for marker in SENSITIVE_MARKERS:
            assert marker not in blob

        # Req 3: the same browser_session_id + attempt_id survive the pause.
        before = env.manager.get(bs.browser_session_id)
        assert before.browser_session_id == bs.browser_session_id
        assert before.attempt_id == started.attempt_id
        assert before.attempt_id is not None

        # Explicit participant approval, then resume: first step submits the
        # licence (Continue), next step observes the quote result. The SAME
        # browser_session_id + attempt_id continue throughout.
        env.manager.approve_checkpoint(bs.browser_session_id, "identity_lookup")
        r4 = await env.manager.step_session(bs.browser_session_id)
        assert r4.observation_type.value == "fields_filled"
        assert r4.status.value == "running"
        assert any(e.action == "click" and e.status == "success" for e in r4.action_events)
        r5 = await env.manager.step_session(bs.browser_session_id)
        assert r5.observation_type.value == "quote_detected"
        assert r5.status.value == "succeeded"
        assert r5.observation.quote.raw.annual_amount_parsed == 1200.0
        after = env.manager.get(bs.browser_session_id)
        assert after.browser_session_id == bs.browser_session_id
        assert after.attempt_id == started.attempt_id
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Req 1: declaration is a TERMINAL PROHIBITED boundary; third-party consent
# is a pre-fill resumable human checkpoint. Neither fills anything.
# ---------------------------------------------------------------------------


async def test_sonnet_declaration_and_consent_boundaries(tmp_path, mock_site) -> None:
    # Application declaration: terminal stop, must_not_automate=True - the
    # applicant must accept it personally; never a Resume/Approve control.
    env = _sonnet_env(tmp_path, mock_site, scenario="declaration")
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        assert result.status.value == "stopped_prohibited"
        checkpoint = result.observation.checkpoint
        assert checkpoint is not None
        assert checkpoint.checkpoint_type == "application_declaration"
        assert checkpoint.requires_human is True
        assert checkpoint.must_not_automate is True
        assert not any(e.action in ("fill", "select", "click") for e in result.action_events)
    finally:
        await _stop(env, bs.browser_session_id)

    # Third-party data consent: pre-fill resumable human checkpoint.
    env = _sonnet_env(tmp_path, mock_site, scenario="consent-attestation")
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        assert result.status.value == "paused_human_checkpoint"
        checkpoint = result.observation.checkpoint
        assert checkpoint is not None
        assert checkpoint.checkpoint_type == "consent_attestation"
        assert checkpoint.requires_human is True
        assert checkpoint.must_not_automate is False
        assert not any(e.action in ("fill", "select", "click") for e in result.action_events)
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Req 6: a missing canonical value pauses instead of blank-filling/clicking
# ---------------------------------------------------------------------------


async def test_sonnet_missing_value_pauses_no_blank_no_continue(tmp_path, mock_site) -> None:
    env = _sonnet_env(
        tmp_path, mock_site, scenario="sonnet",
        persona=make_standard_auto_profile(annual_kilometres=None),
    )
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        r1 = await env.manager.start_session(bs.browser_session_id)
        assert r1.status.value == "paused_needs_field"
        assert r1.observation_type.value == "needs_field"
        assert ANNUAL_KM_PATH in r1.observation.missing_field_paths
        # No Continue click, no blank fill of the missing canonical value.
        assert not any(e.action == "click" for e in r1.action_events)
        assert not any(e.canonical_field == ANNUAL_KM_PATH and e.action in ("fill", "select")
                       for e in r1.action_events)

        # Supply the value via the vault (participant answered the intake
        # request), then resume: the field is FILLED (not blank), and because
        # the screen also carries the licence number, the licence-submission
        # checkpoint fires before any Continue.
        profile_id = env.engine.get_session(env.session_id).profile_id
        profile = env.vault.get(profile_id)
        env.vault.update(profile_id, profile.updated(ANNUAL_KM_PATH, 12000))
        r2 = await env.manager.step_session(bs.browser_session_id)
        assert r2.status.value == "paused_human_checkpoint"
        assert r2.observation.checkpoint.checkpoint_type == "identity_lookup"
        assert any(e.canonical_field == ANNUAL_KM_PATH and e.status == "success"
                   for e in r2.action_events if e.action == "fill")
        assert not any(e.action == "click" for e in r2.action_events)
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Req 2: approval is explicit, per-kind, and never grants must-not-automate
# ---------------------------------------------------------------------------


async def test_sonnet_checkpoint_approval_is_explicit_and_same_session(tmp_path, mock_site) -> None:
    env = _sonnet_env(tmp_path, mock_site, scenario="sonnet")
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        r1 = await env.manager.start_session(bs.browser_session_id)
        assert r1.status.value == "paused_human_checkpoint"
        assert bs.attempt_id is None  # sandbox has no recovery engine

        # The session must NOT be auto-approved: no click yet.
        assert not any(e.action == "click" for e in r1.action_events)

        # Approval is explicit and recorded on the SAME session object.
        approved = env.manager.approve_checkpoint(bs.browser_session_id, "identity_lookup")
        assert approved.browser_session_id == bs.browser_session_id
        assert approved.attempt_id == bs.attempt_id
        assert "identity_lookup" in approved.checkpoint_approvals
        assert approved.status.value == "paused_human_checkpoint"  # still paused until resume

        # MUST-NOT-AUTOMATE checkpoints can never be approved - including the
        # application declaration and payment.
        import pytest

        with pytest.raises(ValueError):
            env.manager.approve_checkpoint(bs.browser_session_id, "payment")
        with pytest.raises(ValueError):
            env.manager.approve_checkpoint(bs.browser_session_id, "application_declaration")

        # An unknown checkpoint kind is rejected.
        with pytest.raises(ValueError):
            env.manager.approve_checkpoint(bs.browser_session_id, "not-a-kind")

        # After approval the resume submits on the same ids, then quotes.
        r2 = await env.manager.step_session(bs.browser_session_id)
        assert r2.observation_type.value == "fields_filled"
        assert r2.status.value == "running"
        assert any(e.action == "click" and e.status == "success" for e in r2.action_events)
        r3 = await env.manager.step_session(bs.browser_session_id)
        assert r3.observation_type.value == "quote_detected"
        assert r3.status.value == "succeeded"
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Req 3 evidence: the licence-submission pause is preserved redacted
# ---------------------------------------------------------------------------


async def test_sonnet_checkpoint_evidence_privacy_safe(tmp_path, mock_site) -> None:
    ev_env, sink = make_sink_env()
    env = _sonnet_env(tmp_path, mock_site, scenario="sonnet-multistep", evidence_sink=sink)
    bs = env.manager.create(env.session_id, "sonnet", BrowserExecutionMode.SANDBOX)
    try:
        await env.manager.start_session(bs.browser_session_id)
        r2 = await env.manager.step_session(bs.browser_session_id)
        r3 = await env.manager.step_session(bs.browser_session_id)
        assert r3.status.value == "paused_human_checkpoint"

        records = await ev_env.repo.list_by_intake(env.session_id)
        assert records
        field_events = [r for r in records if r.event_type is EvidenceEventType.FIELD_INTERACTION_OBSERVED]
        # The licence field interaction is recorded as a canonical PATH only,
        # and the licence was FILLED - never submitted (no click on it).
        licence_events = [r for r in field_events if r.payload.canonical_path == LICENCE_PATH]
        assert licence_events
        assert all(r.payload.action in ("fill", "select") for r in licence_events)
        assert not any(r.payload.action == "click" for r in licence_events)
        assert_evidence_privacy_safe(records)
    finally:
        await _stop(env, bs.browser_session_id)
