"""Issue #10 - privacy tests: sensitive markers never leak into records,
hashes, audit, exports, or URLs; the URL sanitizer strips query/fragment/
userinfo; the payload allowlist backstop rejects unsafe free keys."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.evidence import (
    AuditEventName,
    EvidenceEventType,
    SafeMetadataEvidence,
)
from app.services.evidence.hashing import evidence_content_hash, quote_content_hash
from app.services.evidence.url_sanitizer import safe_url_only, sanitize_url

from evidence_helpers import (
    SENSITIVE_MARKERS,
    QUOTE_REFERENCE,
    make_evidence_env,
    page_observation,
    quote_observation,
)
from intake_helpers import SYNTHETIC_EMAIL, SYNTHETIC_LICENCE, SYNTHETIC_VIN, SYNTHETIC_DOB


# ---------------------------------------------------------------------------
# URL sanitizer (§45)
# ---------------------------------------------------------------------------


def test_sanitize_url_strips_query_fragment_userinfo_and_token() -> None:
    info = sanitize_url(
        "https://user:pass@provider.ca/auto/quote?postal=M5V1A1&token=SECRET#section"
    )
    assert info.host == "provider.ca"
    assert info.path == "/auto/quote"
    assert info.safe_url == "provider.ca/auto/quote"
    for marker in ["M5V1A1", "SECRET", "user:pass", "#section"]:
        assert marker not in (info.safe_url or "")


def test_sanitize_url_keeps_port() -> None:
    assert safe_url_only("http://127.0.0.1:8765/page-a?x=1") == "127.0.0.1:8765/page-a"


def test_sanitize_url_empty_and_bad_input() -> None:
    assert sanitize_url(None).safe_url is None
    assert sanitize_url("").safe_url is None
    assert sanitize_url("not a url").safe_url is not None  # best-effort


# ---------------------------------------------------------------------------
# Full-session PII scan across every stored artifact
# ---------------------------------------------------------------------------


async def _build_full_session(tmp_path: Path):
    env = make_evidence_env()
    await env.service.record_route_planned(
        "intake-1",
        _route_plan(),
    )
    await env.service.record_attempt(
        "intake-1",
        event_type=EvidenceEventType.ATTEMPT_STARTED,
        channel="browser",
        **env.ids(),
    )
    await env.service.record_browser_observation(
        "intake-1", page_observation(), **env.ids(), browser_session_id="bs-1"
    )
    await env.service.record_browser_quote(
        "intake-1", quote_observation(), **env.ids()
    )
    await env.service.record_voice_observation(
        "intake-1",
        voice_session_id="vs-1",
        observation_type="phone_quote_observed",
        plan_id="plan-1",
        planned_route_id="mock-insurer",
        registry_id="mock-insurer",
        distinct_rate_source_id="RS-MOCK-INSURER",
        attempt_id="att-100",
        route_status="quote_pending_normalization",
    )
    await env.service.record_consent(
        "intake-1", plan_id="plan-1", planned_route_id="mock-insurer",
        registry_id="mock-insurer", scope="quote", state="granted",
    )
    await env.service.record_audit_event(
        "intake-1", event_name=AuditEventName.ATTEMPT_TERMINALIZED,
        safe_metadata={"reason_code": "blocked"},
    )
    return env


def _route_plan():
    import datetime as dt

    from app.models.route_planner import (
        InsuranceType,
        PlannedRoute,
        RoutePlan,
        RoutePlanSummary,
    )

    return RoutePlan(
        session_id="plan-1",
        insurance_type=InsuranceType.AUTO,
        routes=[
            PlannedRoute(
                registry_id="mock-insurer", brand_or_program="Mock",
                distribution_type="direct", product_scope="standard_PPA",
                deduplication_status="primary", route_status="ready", is_ready=True, rank=1,
            )
        ],
        summary=RoutePlanSummary(),
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )


async def test_no_sensitive_markers_in_records_hashes_audit_or_export(tmp_path: Path) -> None:
    env = await _build_full_session(tmp_path)
    records = await env.service.list_by_intake("intake-1")
    quotes = await env.service.list_quote_observations("intake-1")
    audit = await env.service.list_audit_events("intake-1")
    export = await env.service.export("intake-1")

    blobs = []
    for r in records:
        blobs.append(r.model_dump_json())
        blobs.append(evidence_content_hash(r.model_dump()))
        blobs.append(r.idempotency_key)
        blobs.append(r.content_hash)
    for q in quotes:
        blobs.append(q.model_dump_json())
        blobs.append(quote_content_hash(q.model_dump()))
        blobs.append(q.idempotency_key)
        blobs.append(q.content_hash)
    for a in audit:
        blobs.append(a.model_dump_json())
        blobs.append(a.content_hash)
        blobs.append(a.idempotency_key)
    blobs.append(export.model_dump_json())

    for marker in SENSITIVE_MARKERS + [QUOTE_REFERENCE, SYNTHETIC_LICENCE, SYNTHETIC_VIN, SYNTHETIC_DOB]:
        for blob in blobs:
            assert marker not in blob, f"sensitive marker leaked: {marker!r} in {blob[:300]}"


async def test_raw_quote_reference_never_in_payload_or_handle(tmp_path: Path) -> None:
    env = await _build_full_session(tmp_path)
    blob = (await env.service.export("intake-1")).model_dump_json()
    assert QUOTE_REFERENCE not in blob
    # The opaque handle is allowed; the raw reference value is not.
    quotes = await env.service.list_quote_observations("intake-1")
    for q in quotes:
        assert q.private_reference_handle != QUOTE_REFERENCE


async def test_payload_allowlist_backstop_rejects_unsafe_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SafeMetadataEvidence(
            safe_metadata={"legal_name": "Test Applicant", "reason_code": "blocked"}
        )
    # Only allowlisted keys pass.
    safe = SafeMetadataEvidence(safe_metadata={"reason_code": "blocked", "config_version": 1})
    assert safe.safe_metadata["reason_code"] == "blocked"


async def test_evidence_repr_and_json_never_reveal_applicant_values(tmp_path: Path) -> None:
    env = await _build_full_session(tmp_path)
    for r in await env.service.list_by_intake("intake-1"):
        assert SYNTHETIC_LICENCE not in repr(r)
        assert SYNTHETIC_EMAIL not in repr(r)
