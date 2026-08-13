"""Comparison-run route summaries expose the preserved redacted Square One
evidence (callback_required) GENERICALLY, without rerunning any provider.

Proves (hermetic - local mock site only, no real provider, no LLM, no LangSmith):
- the preserved Square One callback_observed record's timestamp / id / hash /
  safe URL / terminal reason / quote_count flow through the route summary
  unchanged (seeded from the exact preserved fixture values);
- status stays ``callback_required`` (never relabelled blocked/denied/CAPTCHA/
  quoted) and ``annual_premium`` stays None (the frontend renders an em dash);
- the summary carries ONLY safe redacted metadata (no applicant information);
- routes without evidence continue working (fields stay unavailable/None).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models.comparison_run import (
    ComparisonRun,
    ComparisonRunStatus,
    RouteRunStatus,
    RouteRunSummary,
)
from app.models.evidence import (
    CheckpointEvidence,
    EvidenceEventType,
    EvidenceRecord,
    RecoveryEvidence,
)
from app.models.recovery import SourceChannel

from comparison_run_helpers import make_comparison_run_env
from evidence_helpers import SENSITIVE_MARKERS, assert_evidence_privacy_safe

pytestmark = pytest.mark.usefixtures("mock_site")

# Exact values preserved from the genuine controlled LIVE Square One run.
SQUARE_ONE_EVIDENCE_ID = "5bde8934a1f844ee8339bcdf9d248e6d"
SQUARE_ONE_HASH = "d21abc04269483e1f6176846f4caeccdba7fd92b6d9271b7020648bea98c5598"
SQUARE_ONE_ATTEMPT_ID = "606d629ee9954503a5e6444cc97611d3"
CALLBACK_OBSERVED_AT = dt.datetime(2026, 8, 12, 21, 21, 45, 197592, tzinfo=dt.timezone.utc)
SAFE_SOURCE_URL = "www.squareone.ca/auto-insurance/"


async def _seed_square_one_callback(env, sid: str) -> list[EvidenceRecord]:
    """Insert the exact preserved records into the in-memory repo.

    ``repo.append`` preserves the given evidence_id and content_hash (unlike
    ``EvidenceService.append`` which regenerates them), so the summary must
    surface the EXACT preserved values - no fabrication, no re-derivation.
    """
    repo = env.evidence._repo  # same store the run service reads
    callback = EvidenceRecord(
        evidence_id=SQUARE_ONE_EVIDENCE_ID,
        event_type=EvidenceEventType.CALLBACK_OBSERVED,
        observed_at=CALLBACK_OBSERVED_AT,
        created_at=CALLBACK_OBSERVED_AT,
        intake_session_id=sid,
        plan_id=f"plan-{sid}",
        planned_route_id="square-one",
        registry_id="square-one",
        distinct_rate_source_id="RS-ZURICH-AUTO",
        attempt_id=SQUARE_ONE_ATTEMPT_ID,
        source_channel=SourceChannel.BROWSER,
        source_session_id="275f4513787f46a690a627d223136ba2",
        page_signature="square-one_landing",
        safe_url=SAFE_SOURCE_URL,
        observation_type="callback_detected",
        evidence_source="evidence_service",
        payload=CheckpointEvidence(
            checkpoint_type="callback",
            automation_decision="escalate",
            must_not_automate=False,
            requires_human=True,
        ),
        content_hash=SQUARE_ONE_HASH,
        idempotency_key=f"seed|callback|{sid}",
    )
    recovery = EvidenceRecord(
        evidence_id="669f27fbf00e47de8c0b636d96c786f6",
        event_type=EvidenceEventType.RECOVERY_DECISION,
        observed_at=dt.datetime(2026, 8, 12, 21, 21, 45, 203591, tzinfo=dt.timezone.utc),
        created_at=dt.datetime(2026, 8, 12, 21, 21, 45, 203591, tzinfo=dt.timezone.utc),
        intake_session_id=sid,
        plan_id=f"plan-{sid}",
        planned_route_id="square-one",
        registry_id="square-one",
        distinct_rate_source_id="RS-ZURICH-AUTO",
        attempt_id=SQUARE_ONE_ATTEMPT_ID,
        source_channel=SourceChannel.MANUAL,
        evidence_source="recovery_engine",
        payload=RecoveryEvidence(
            lifecycle_status="terminal",
            recommended_action="prepare_voice_handoff",
            reason_codes=["callback_required"],
            terminal_status="callback_required",
            quote_pending_normalization=False,
            policy_version="1",
            retry_allowed=False,
        ),
        content_hash="320c4c116adba08bf3b02208a6aec9ad75fdbc8c239d910c1d6117731eb0230c",
        idempotency_key=f"seed|recovery|{sid}",
    )
    await repo.append(callback)
    await repo.append(recovery)
    return [callback, recovery]


def _square_one_run(sid: str, now: dt.datetime) -> ComparisonRun:
    return ComparisonRun(
        comparison_run_id="run-square-one",
        intake_session_id=sid,
        plan_id=f"plan-{sid}",
        execution_mode="live",
        status=ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS,
        created_at=now,
        completed_at=now,
        route_summaries=[
            RouteRunSummary(
                registry_id="square-one",
                display_name="Square One",
                channel="browser",
                status=RouteRunStatus.CALLBACK_REQUIRED,
                route_outcome_semantics="callback_required",
                terminal_status="callback_required",
                reason_codes=["callback_required"],
                distinct_rate_source_id="RS-ZURICH-AUTO",
            ),
        ],
    )


async def _attached_square_one(env, sid: str, run: ComparisonRun):
    await env.run_service.attach_evidence(run)
    stored = env.run_service.get_run(sid, run.comparison_run_id)
    assert stored is not None
    return stored.route_summaries[0]


async def test_preserved_square_one_callback_evidence_unchanged(tmp_path, mock_site) -> None:
    env = make_comparison_run_env(
        tmp_path, mock_site,
        routes=[("square-one", "Square One", "quote", "RS-ZURICH-AUTO", "direct")],
    )
    sid = env.session_id
    seeded = await _seed_square_one_callback(env, sid)
    now = dt.datetime(2026, 8, 12, 21, 30, 0, tzinfo=dt.timezone.utc)
    s = await _attached_square_one(env, sid, _square_one_run(sid, now))

    assert s.evidence_status == "recorded"
    # Exact preserved values - the timestamp is returned unchanged (Z-suffixed
    # UTC ISO), never re-derived or fabricated.
    assert s.evidence_observed_at == "2026-08-12T21:21:45.197592Z"
    assert s.evidence_id == SQUARE_ONE_EVIDENCE_ID
    assert s.evidence_content_hash == SQUARE_ONE_HASH
    assert s.safe_source_url == SAFE_SOURCE_URL
    assert s.terminal_reason == "callback_required"
    assert s.quote_count == 0
    # Seeded records themselves are privacy-safe (PII-capable content only).
    assert_evidence_privacy_safe(seeded)


async def test_status_stays_callback_required_not_relabelled(tmp_path, mock_site) -> None:
    env = make_comparison_run_env(
        tmp_path, mock_site,
        routes=[("square-one", "Square One", "quote", "RS-ZURICH-AUTO", "direct")],
    )
    sid = env.session_id
    await _seed_square_one_callback(env, sid)
    now = dt.datetime(2026, 8, 12, 21, 30, 0, tzinfo=dt.timezone.utc)
    s = await _attached_square_one(env, sid, _square_one_run(sid, now))

    # Status must stay exactly callback_required.
    assert s.status == RouteRunStatus.CALLBACK_REQUIRED
    assert s.terminal_status == "callback_required"
    assert s.route_outcome_semantics == "callback_required"
    forbidden = {
        RouteRunStatus.CAPTCHA_BLOCKED, RouteRunStatus.UNAVAILABLE,
        RouteRunStatus.COMPARABLE, RouteRunStatus.NON_COMPARABLE,
        RouteRunStatus.ESTIMATE_ONLY, RouteRunStatus.FAILED,
        RouteRunStatus.MANUAL_HANDOFF,
    }
    assert s.status not in forbidden


async def test_no_premium_and_zero_quote_count(tmp_path, mock_site) -> None:
    env = make_comparison_run_env(
        tmp_path, mock_site,
        routes=[("square-one", "Square One", "quote", "RS-ZURICH-AUTO", "direct")],
    )
    sid = env.session_id
    await _seed_square_one_callback(env, sid)
    now = dt.datetime(2026, 8, 12, 21, 30, 0, tzinfo=dt.timezone.utc)
    s = await _attached_square_one(env, sid, _square_one_run(sid, now))

    # The frontend renders an em dash for annual_premium=None; no number.
    assert s.annual_premium is None
    assert s.quote_count == 0
    assert s.quote_observation_id is None
    assert s.normalized_quote_id is None


async def test_evidence_fields_are_safe_redacted_metadata_only(tmp_path, mock_site) -> None:
    env = make_comparison_run_env(
        tmp_path, mock_site,
        routes=[("square-one", "Square One", "quote", "RS-ZURICH-AUTO", "direct")],
    )
    sid = env.session_id
    await _seed_square_one_callback(env, sid)
    now = dt.datetime(2026, 8, 12, 21, 30, 0, tzinfo=dt.timezone.utc)
    s = await _attached_square_one(env, sid, _square_one_run(sid, now))

    # The summary exposes ONLY the safe metadata - no applicant markers, no
    # request payloads, no cookies/tokens/browser storage.
    blob = " ".join(
        str(x) for x in [
            s.evidence_observed_at, s.evidence_id, s.evidence_content_hash,
            s.safe_source_url, s.terminal_reason, s.display_name,
        ] if x is not None
    )
    for marker in SENSITIVE_MARKERS:
        assert marker not in blob, marker


async def test_routes_without_evidence_continue_working(tmp_path, mock_site) -> None:
    env = make_comparison_run_env(
        tmp_path, mock_site,
        routes=[("mock-provider-x", "Mock Provider X", "quote-estimate", "RS-MOCK-X", "direct")],
    )
    sid = env.session_id
    now = dt.datetime(2026, 8, 12, 21, 30, 0, tzinfo=dt.timezone.utc)
    run = ComparisonRun(
        comparison_run_id="run-no-evidence",
        intake_session_id=sid,
        plan_id=f"plan-{sid}",
        execution_mode="mock",
        status=ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS,
        created_at=now,
        completed_at=now,
        route_summaries=[
            RouteRunSummary(
                registry_id="mock-provider-x",
                display_name="Mock Provider X",
                status=RouteRunStatus.ESTIMATE_ONLY,
                route_outcome_semantics="estimate_only",
                terminal_status="estimate_only",
                reason_codes=["estimate_only"],
                distinct_rate_source_id="RS-MOCK-X",
            ),
        ],
    )
    await env.run_service.attach_evidence(run)
    # For routes with NO evidence, attach_evidence makes no changes and does
    # not persist anything new - the passed run's summary is untouched.
    s = run.route_summaries[0]
    # No evidence -> fields stay unavailable/None, status untouched.
    assert s.evidence_status == "unavailable"
    assert s.evidence_id is None
    assert s.evidence_observed_at is None
    assert s.evidence_content_hash is None
    assert s.safe_source_url is None
    assert s.terminal_reason is None
    assert s.quote_count == 0
    assert s.status == RouteRunStatus.ESTIMATE_ONLY


def test_route_summary_accepts_safe_evidence_fields() -> None:
    """Model-level: the summary carries the new optional safe fields."""
    s = RouteRunSummary(
        registry_id="square-one",
        display_name="Square One",
        status=RouteRunStatus.CALLBACK_REQUIRED,
        evidence_status="recorded",
        evidence_observed_at="2026-08-12T21:21:45.197592Z",
        evidence_id=SQUARE_ONE_EVIDENCE_ID,
        evidence_content_hash=SQUARE_ONE_HASH,
        safe_source_url=SAFE_SOURCE_URL,
        terminal_reason="callback_required",
        quote_count=0,
    )
    data = s.model_dump(mode="json")
    assert data["evidence_observed_at"] == "2026-08-12T21:21:45.197592Z"
    assert data["evidence_id"] == SQUARE_ONE_EVIDENCE_ID
    assert data["safe_source_url"] == SAFE_SOURCE_URL
    assert data["terminal_reason"] == "callback_required"
    assert data["quote_count"] == 0
    assert data["annual_premium"] is None
