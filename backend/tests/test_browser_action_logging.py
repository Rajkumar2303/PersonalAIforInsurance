"""Privacy-safe browser-action logging - hermetic tests.

The generic browser executor emits a structured, redacted ``BrowserActionEvent``
for navigate / fill / select / click / pause / extract. Each event carries ONLY
provider (registry id), the canonical_field PATH, action, status, and safe
correlation ids (request/trace/attempt/plan/browser-session) + timestamp. It
must NEVER contain the entered value, a selector, page text, URL query params,
cookies, tokens, raw Playwright logs, or screenshots.

These tests prove that: events are emitted for all six actions; known applicant
markers never appear in event content, structured logs, or the redacted
evidence timeline; canonical_field is a PATH (never a value); and Playwright
API logging (pw:api) is never enabled. Hermetic (local mock quote site only).
"""

from __future__ import annotations

import inspect
import logging
import re

import pytest

from app.browser.mock_site import MOCK_REGISTRY_ID
from app.browser.session import BrowserExecutionMode
from app.models.evidence import EvidenceEventType

from browser_helpers import make_browser_env
from evidence_helpers import (
    SENSITIVE_MARKERS,
    assert_evidence_privacy_safe,
    make_sink_env,
)
from personas import make_standard_auto_profile

ALL_ACTIONS = {"navigate", "fill", "select", "click", "pause", "extract"}


def _event_content(event) -> str:
    """Only the applicant-controlled content fields (never opaque ids)."""
    return "\n".join(
        [event.provider, event.action, event.canonical_field or "", event.status]
    )


def _serialized_events(events) -> str:
    return "\n".join(e.model_dump_json() for e in events)


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
# Events are emitted for the required actions
# ---------------------------------------------------------------------------


async def test_actions_navigate_fill_click_emitted(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="applicant")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        actions = {e.action for e in result.action_events}
        assert "navigate" in actions
        assert "click" in actions
        assert actions & {"fill", "select"}  # canonical fields were filled
        for ev in result.action_events:
            assert ev.provider == MOCK_REGISTRY_ID
            assert ev.observed_at is not None
            if ev.canonical_field:
                assert ev.canonical_field.startswith(("applicant.", "product_data."))
    finally:
        await _stop(env, bs.browser_session_id)


async def test_pause_event_on_missing_field(tmp_path, mock_site) -> None:
    persona = make_standard_auto_profile(annual_kilometres=None)
    env = make_browser_env(tmp_path, mock_site, scenario="multi-missing", persona=persona)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        pauses = [e for e in result.action_events if e.action == "pause"]
        assert pauses and pauses[-1].status == "paused"
    finally:
        await _stop(env, bs.browser_session_id)


async def test_extract_event_callback_blocked_no_quote(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="callback")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        extracts = [e for e in result.action_events if e.action == "extract"]
        assert extracts and extracts[-1].status == "blocked"
        assert result.observation.quote is None  # no premium fabricated
    finally:
        await _stop(env, bs.browser_session_id)


async def test_extract_event_quote_success(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="quote-annual")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        extracts = [e for e in result.action_events if e.action == "extract"]
        assert extracts and extracts[-1].status == "success"
        assert result.observation.quote is not None
        assert result.observation.quote.raw.annual_amount_parsed == 1200.0
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Privacy: never values / selectors / text / ids in event CONTENT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["applicant", "callback", "captcha", "quote-annual"])
async def test_event_content_never_contains_applicant_markers(scenario, tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario=scenario)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        assert result.action_events
        content = "\n".join(_event_content(e) for e in result.action_events)
        for marker in SENSITIVE_MARKERS:
            assert marker not in content
    finally:
        await _stop(env, bs.browser_session_id)


async def test_canonical_field_is_path_never_value(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site, scenario="applicant")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        fills = [
            e for e in result.action_events
            if e.action in ("fill", "select") and e.canonical_field
        ]
        assert fills
        for ev in fills:
            assert ev.canonical_field.startswith(("applicant.", "product_data."))
            assert "=" not in ev.canonical_field  # never a value/query shape
            # a literal applicant value is never present alongside the path
            assert re.fullmatch(r"[A-Za-z0-9_.\[\]]+", ev.canonical_field) is not None
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Structured backend logs (redacted) - example shape + no markers
# ---------------------------------------------------------------------------


async def test_structured_log_shape_and_privacy(tmp_path, mock_site, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.browser.executor")
    env = make_browser_env(tmp_path, mock_site, scenario="applicant")
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await env.manager.start_session(bs.browser_session_id)
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("browser_action")
        ]
        assert lines
        # Example shape: provider=sonnet action=fill canonical_field=... status=success
        assert re.search(
            r"provider=\S+ action=\S+ canonical_field=\S+ status=\S+", lines[0]
        )
        for line in lines:
            for marker in SENSITIVE_MARKERS:
                assert marker not in line
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# Preserved in the redacted evidence timeline (field_interaction_observed)
# ---------------------------------------------------------------------------


async def test_action_events_preserved_in_redacted_evidence_timeline(tmp_path, mock_site) -> None:
    ev_env, sink = make_sink_env()
    env = make_browser_env(tmp_path, mock_site, scenario="applicant", evidence_sink=sink)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        result = await env.manager.start_session(bs.browser_session_id)
        assert result.action_events, "expected action events on the step result"
        records = await ev_env.repo.list_by_intake(env.session_id)
        field_events = [
            r for r in records if r.event_type is EvidenceEventType.FIELD_INTERACTION_OBSERVED
        ]
        assert len(field_events) >= len(result.action_events)
        for record in field_events:
            assert record.payload.action in ALL_ACTIONS
            assert record.payload.status in ("success", "failure", "paused", "blocked", "skipped")
            if record.payload.canonical_path:
                assert record.payload.canonical_path.startswith(("applicant.", "product_data."))
        # The preserved timeline is redacted - no applicant values anywhere.
        assert_evidence_privacy_safe(records)
    finally:
        await _stop(env, bs.browser_session_id)


# ---------------------------------------------------------------------------
# DEBUG=pw:api is never enabled
# ---------------------------------------------------------------------------


def test_playwright_api_logging_never_enabled() -> None:
    # We must NEVER enable Playwright protocol logging (raw protocol output).
    assert logging.getLogger("pw:api").level != logging.DEBUG
    # Guard: the executor + session manager never reference/configure pw:api.
    for module_name in ("app.browser.executor", "app.browser.session"):
        module = __import__(module_name, fromlist=["*"])
        source = inspect.getsource(module)
        assert "pw:api" not in source, f"{module_name} must not touch pw:api"
