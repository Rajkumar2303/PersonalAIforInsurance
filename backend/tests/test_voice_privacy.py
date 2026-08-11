"""Issue #9 Prompt 2 - voice privacy hardening tests (hermetic).

Proves sensitive applicant data (licence, VIN, DOB, address, email, applicant
phone, claim details, quote reference) NEVER persists in voice sessions,
decisions, graph state, recovery records, logs, or API responses. Values are
read JIT from the Issue #5 intake vault, spoken through the transport boundary,
and DISCARDED immediately after use (Prompt 2) - the transport's raw spoken
text is retained only in test-only ``retain_transcript`` transports and is
absent everywhere in the default (privacy) configuration.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.voice import BrokerQuestionKind, VoiceResponseAction
from intake_helpers import (
    SYNTHETIC_DOB,
    SYNTHETIC_EMAIL,
    SYNTHETIC_LICENCE,
    SYNTHETIC_POSTAL,
    SYNTHETIC_STREET,
    SYNTHETIC_VIN,
)
from route_planner_helpers import complete_starter
from voice_helpers import field_question, kind_question, make_voice_env, prepare_and_disclose

SENSITIVE_MARKERS = [
    SYNTHETIC_LICENCE,
    SYNTHETIC_VIN,
    SYNTHETIC_DOB,
    SYNTHETIC_STREET,
    SYNTHETIC_POSTAL,
    SYNTHETIC_EMAIL,
]

# Applicant phone / claim-detail / quote-reference markers (Prompt 2 scope).
APPLICANT_PHONE = "416-555-0199"
CLAIM_DETAILS = "rear-ended in 2019 at 5th and Main"
QUOTE_REFERENCE = "Q-2026-8844-AB"

# Extra markers never supplied by the app; must never appear even in raw paths.
EXTRA_MARKERS = [APPLICANT_PHONE, CLAIM_DETAILS, QUOTE_REFERENCE]


def _scan(obj, markers=SENSITIVE_MARKERS) -> list[str]:
    """Return any sensitive markers found in a dumped object tree."""
    return [m for m in markers if m in str(obj)]


def test_extra_sensitive_markers_never_appear(tmp_path):
    # Simulate a broker asking for the applicant's phone / claim details, and
    # a quote reference being observed - none may persist anywhere.
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    env.engine.receive_broker_event(
        session.voice_session_id,
        kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE, text=QUOTE_REFERENCE),
    )
    artifacts = [
        env.engine.get(session.voice_session_id).model_dump(),
        [a.model_dump() for a in env.recovery_store.list_all()],
        env.engine.route_summaries(env.session_id)[0].model_dump(),
    ]
    for artifact in artifacts:
        for marker in EXTRA_MARKERS:
            assert marker not in str(artifact), f"leaked: {marker}"


def test_value_discarded_after_use_not_persisted(tmp_path):
    # Default transport (no transcript): the value must be discarded after use.
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    decision = env.engine.receive_broker_event(
        session.voice_session_id, field_question("applicant.address.postal_code")
    )
    assert decision.action == VoiceResponseAction.DISCLOSE_VALUE
    # The value WAS spoken then DISCARDED: nothing lingers on the transport.
    assert env.transport.last_spoken is None
    assert env.transport.last_spoken_path == "applicant.address.postal_code"
    # Not persisted anywhere in the voice layer.
    assert _scan(env.engine.get(session.voice_session_id).model_dump()) == []
    assert _scan(decision.model_dump()) == []
    assert _scan(env.store.get(session.voice_session_id).model_dump()) == []
    # The transport boundary stays cleanable.
    env.transport.clear_last_spoken()
    assert env.transport.last_spoken is None


def test_value_flows_only_through_test_transcript(tmp_path):
    # Test-only retained transcript proves the JIT value genuinely reached the
    # broker (the value is the SPOKEN text, then discarded from last_spoken).
    env = make_voice_env(tmp_path, retain_transcript=True)
    session = prepare_and_disclose(env)
    env.engine.receive_broker_event(
        session.voice_session_id, field_question("applicant.address.postal_code")
    )
    assert SYNTHETIC_POSTAL in "".join(env.transport.spoken)
    assert env.transport.last_spoken is None  # discarded after use


def test_full_flow_leaks_nothing_to_logs_or_recovery(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    env.engine.receive_broker_event(
        session.voice_session_id, field_question("applicant.address.postal_code")
    )
    # Trigger recovery observations across a few terminal kinds.
    env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE)
    )
    env.engine.receive_broker_event(
        session.voice_session_id, kind_question(BrokerQuestionKind.CALLBACK_REQUEST)
    )
    # Recovery attempt records/decisions must not contain values.
    for attempt in env.recovery_store.list_all():
        assert _scan(attempt.model_dump()) == []
    # Logs must not contain values.
    assert _scan(caplog.text) == []


def test_privacy_scan_over_all_voice_artifacts(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    # A missing-field request (Issues #5) touches the intake layer too.
    path = "product_data.vehicles[0].use.annual_kilometres"
    env.engine.receive_broker_event(session.voice_session_id, field_question(path))
    env.intake.submit_answer(env.session_id, path, 15000)
    env.engine.resume(session.voice_session_id)
    artifacts = [
        env.engine.get(session.voice_session_id).model_dump(),
        env.store.list_all(),
        [a.model_dump() for a in env.recovery_store.list_all()],
        env.intake.get_session(env.session_id).model_dump(),
    ]
    for artifact in artifacts:
        assert _scan(artifact) == []


def test_api_responses_never_leak_values(client: TestClient):
    """End-to-end through the FastAPI app (singleton engines, synthetic data)."""
    from app.models.insurance.enums import InsuranceType
    from app.services.intake import get_intake_engine

    intake = get_intake_engine()
    session, _gate = intake.create_session(InsuranceType.AUTO)
    complete_starter(intake, session.session_id)
    # Use a real market-registry id so grant_route_consent resolves.
    intake.grant_route_consent(session.session_id, "sonnet", [], True)

    handoff = client.post(
        "/api/v1/voice/handoffs",
        json={
            "intake_session_id": session.session_id,
            "registry_id": "sonnet",
            "distinct_rate_source_id": "RS-API",
            "planned_route_id": "route-sonnet",
            "provider_phone_route": "1-800-MOCK-PROVIDER",
        },
    )
    assert handoff.status_code == 200, handoff.text
    body = handoff.json()
    assert _scan(body) == []
    voice_session_id = body["voice_session_id"]

    d = client.post(f"/api/v1/voice/sessions/{voice_session_id}/disclosure", json={"granted": True})
    assert d.status_code == 200
    assert _scan(d.json()) == []

    ev = client.post(
        f"/api/v1/voice/sessions/{voice_session_id}/events",
        json={
            "kind": BrokerQuestionKind.CANONICAL_FIELD.value,
            "canonical_path": "applicant.address.postal_code",
            "raw_safe_text": "postal code",
        },
    )
    assert ev.status_code == 200, ev.text
    assert _scan(ev.json()) == []

    got = client.get(f"/api/v1/voice/sessions/{voice_session_id}")
    assert got.status_code == 200
    assert _scan(got.json()) == []

    # Prompt 2: route-local pause + orchestrator summaries stay safe.
    paused = client.post(f"/api/v1/voice/sessions/{voice_session_id}/pause", json={})
    assert paused.status_code == 200
    assert _scan(paused.json()) == []
    resumed = client.post(f"/api/v1/voice/sessions/{voice_session_id}/resume", json={})
    assert resumed.status_code == 200
    assert _scan(resumed.json()) == []
    summaries = client.get(f"/api/v1/voice/summaries?intake_session_id={session.session_id}")
    assert summaries.status_code == 200
    assert _scan(summaries.json()) == []

    obs = client.post(
        f"/api/v1/voice/sessions/{voice_session_id}/observations",
        json={"observation_type": "phone_quote_observed", "reason": "annual premium quoted"},
    )
    assert obs.status_code == 200, obs.text
    assert _scan(obs.json()) == []
    # Issue #8 authority: quote pending normalization, never comparable.
    assert obs.json()["quote_pending_normalization"] is True


def test_exceptions_never_leak_applicant_values(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    try:
        env.engine.receive_broker_event(
            session.voice_session_id,
            kind_question(BrokerQuestionKind.CANONICAL_FIELD, text=APPLICANT_PHONE),
        )
    except Exception as exc:  # pragma: no cover - we assert nothing leaks
        assert QUOTE_REFERENCE not in str(exc)
        assert APPLICANT_PHONE not in str(exc)
    # Even if the broker asked with a sensitive string, no marker persists.
    persisted = env.engine.get(session.voice_session_id).model_dump()
    assert _scan(persisted, markers=SENSITIVE_MARKERS + EXTRA_MARKERS) == []


def test_voice_engine_never_stores_value_in_session(tmp_path):
    env = make_voice_env(tmp_path)
    session = prepare_and_disclose(env)
    env.engine.receive_broker_event(
        session.voice_session_id, field_question("applicant.address.postal_code")
    )
    persisted = env.engine.get(session.voice_session_id)
    assert SYNTHETIC_POSTAL not in str(persisted.model_dump())
    assert not any(SYNTHETIC_POSTAL in str(p) for p in persisted.pending_field_paths)
