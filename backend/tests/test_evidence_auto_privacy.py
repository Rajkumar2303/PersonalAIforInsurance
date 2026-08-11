"""Issue #10, Prompt 2 - automatic-emission privacy & exception safety (§33/§38/§20).

Scans FULLY automatically generated records/export for synthetic sensitive
markers (name/DOB/licence/VIN/street/postal/email/phone/claims/raw reference).
Premium amounts explicitly modeled as quote observations are allowed. Also
proves persistence exceptions never leak PII, and privacy checkpoints record
only a safe kind (never raw banner DOM/text).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.models.browser.observation import (
    BrowserCheckpointObservation,
    BrowserObservation,
    BrowserObservationType,
)
from app.models.evidence import EvidenceEventType
from app.services.evidence.repository import InMemoryEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.evidence.sink import EvidenceServiceSink, EvidenceWriteStatus
from app.services.evidence.ingest import EvidenceDraft
from app.models.evidence import PageObservationEvidence

from evidence_helpers import (
    SENSITIVE_MARKERS,
    QUOTE_REFERENCE,
    make_sink_env,
)
from intake_helpers import SYNTHETIC_LICENCE, SYNTHETIC_EMAIL, SYNTHETIC_VIN, SYNTHETIC_DOB
from voice_helpers import (
    make_voice_env,
    prepare_and_disclose,
    scripted_happy_path_questions,
)


# ---------------------------------------------------------------------------
# §38 - PII meta-test over fully automatically generated records
# ---------------------------------------------------------------------------


async def test_auto_emitted_voice_records_never_leak(tmp_path: Path) -> None:
    env, sink = make_sink_env()
    venv = make_voice_env(tmp_path, events=scripted_happy_path_questions(), evidence_sink=sink)
    session = prepare_and_disclose(venv)
    while True:
        q = venv.transport.receive_event(session.voice_session_id)
        if q is None:
            break
        venv.engine.receive_broker_event(session.voice_session_id, q)

    records = await env.service.list_by_intake(venv.session_id)
    quotes = await env.service.list_quote_observations(venv.session_id)
    export = await env.service.export(venv.session_id)

    blobs = []
    for r in records:
        blobs.append(r.model_dump_json())
        blobs.append(r.content_hash)
        blobs.append(r.idempotency_key)
    for q in quotes:
        blobs.append(q.model_dump_json())
        blobs.append(q.content_hash)
    blobs.append(export.model_dump_json())

    for marker in SENSITIVE_MARKERS + [QUOTE_REFERENCE, SYNTHETIC_LICENCE, SYNTHETIC_VIN, SYNTHETIC_DOB]:
        for blob in blobs:
            assert marker not in blob, f"leaked {marker!r} in {blob[:300]}"
    # Premium amounts ARE explicitly modeled as quote observations (allowed).
    assert quotes and quotes[0].firm_vs_estimate == "firm"


async def test_auto_emitted_browser_records_never_leak(tmp_path, mock_site) -> None:
    from app.browser.mock_site import MOCK_REGISTRY_ID
    from app.graph.browser_workflow import build_browser_workflow
    from app.models.browser.session import BrowserExecutionMode
    from app.services.recovery.engine import RecoveryEngine
    from browser_helpers import make_browser_env

    env, sink = make_sink_env()
    recovery = RecoveryEngine(evidence_sink=sink)
    benv = make_browser_env(tmp_path, mock_site, evidence_sink=sink, recovery=recovery)
    bs = benv.manager.create(benv.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await build_browser_workflow(benv.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 15}
        )
        records = await env.service.list_by_intake(benv.session_id)
        quotes = await env.service.list_quote_observations(benv.session_id)
        export = await env.service.export(benv.session_id)
        blobs = [r.model_dump_json() for r in records]
        blobs += [r.content_hash for r in records]
        blobs += [q.model_dump_json() for q in quotes]
        blobs.append(export.model_dump_json())
        for marker in SENSITIVE_MARKERS + [QUOTE_REFERENCE]:
            for blob in blobs:
                assert marker not in blob
        # Exact Decimal premium persisted (allowed + precise).
        assert quotes and quotes[0].annual_premium == Decimal("1234.56")
    finally:
        try:
            await benv.manager.close(bs.browser_session_id)
        except Exception:
            pass
        try:
            await benv.browser_manager.stop()
        except Exception:
            pass
        sink.close()


# ---------------------------------------------------------------------------
# §33 - persistence exceptions never leak PII
# ---------------------------------------------------------------------------


class _PoisonedRepo(InMemoryEvidenceRepository):
    """Raises with a PII-laden message to prove it never reaches callers."""

    async def append(self, record):
        raise RuntimeError(f"db connection lost while writing {SYNTHETIC_LICENCE} {SYNTHETIC_EMAIL}")


def test_persistence_exception_never_leaks_pii() -> None:
    service = EvidenceService(_PoisonedRepo())
    sink = EvidenceServiceSink(service)
    result = sink.record(
        "intake-1",
        EvidenceDraft(
            event_type=EvidenceEventType.PAGE_OBSERVED,
            payload=PageObservationEvidence(page_signature="sig"),
            plan_id="plan-1", planned_route_id="mock-insurer", registry_id="mock-insurer",
            distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1",
        ),
    )
    assert result.status is EvidenceWriteStatus.PERSISTENCE_FAILED
    # error_category is the exception TYPE only - never the message/args.
    assert result.error_category == "RuntimeError"
    assert result.error_category != SYNTHETIC_LICENCE
    for marker in [SYNTHETIC_LICENCE, SYNTHETIC_EMAIL, SYNTHETIC_VIN]:
        assert marker not in repr(result)
        assert marker not in (result.error_category or "")
    assert sink.evidence_status() == EvidenceWriteStatus.PERSISTENCE_FAILED.value


# ---------------------------------------------------------------------------
# §20 - privacy checkpoint records only a safe kind (never raw banner DOM)
# ---------------------------------------------------------------------------


async def test_privacy_checkpoint_evidence_never_stores_banner_text() -> None:
    env, sink = make_sink_env()
    obs = BrowserObservation(
        observation_type=BrowserObservationType.HUMAN_CHECKPOINT,
        page_signature="privacy-banner-page",
        url="https://insurer.example.com/cookies",
        message="Privacy banner shown: 'We use cookies to personalise...'",
        checkpoint=BrowserCheckpointObservation(
            checkpoint_type="privacy_banner",
            label="We use cookies and similar technologies to improve your experience...",
            requires_human=True,
            must_not_automate=True,
            action_label="Accept all cookies",
        ),
    )
    from app.services.evidence.ingest import browser_draft_from_observation

    draft = browser_draft_from_observation(
        "intake-1", obs, browser_session_id="bs-1", plan_id="plan-1",
        planned_route_id="mock-insurer", registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER", attempt_id="att-1",
    )
    record = await env.service.append("intake-1", draft)
    assert record.event_type is EvidenceEventType.CHECKPOINT_OBSERVED
    blob = record.model_dump_json()
    # Only the safe checkpoint kind is recorded - never the banner text/label.
    assert record.payload.checkpoint_type == "privacy_banner"
    assert "We use cookies" not in blob
    assert "Accept all cookies" not in blob
    assert "personalise" not in blob
