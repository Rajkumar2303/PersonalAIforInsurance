"""Shared helpers for Issue #10 evidence tests.

All evidence tests are hermetic: no real insurer/telephony/LLM calls, no
LangSmith uploads, no applicant data. Evidence is exercised through the
``EvidenceService`` adapters against either the in-memory repository (unit
tests) or a SQLite-backed repository (persistence/lineage tests).

Sensitive markers assert that licence/VIN/DOB/street/email/phone/claims/raw
quote references never leak into records, hashes, audit, API views, or exports.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from app.models.browser.observation import (
    BrowserObservation,
    BrowserObservationType,
    BrowserQuoteObservation,
    RawQuoteObservation,
)
from app.services.evidence.repository import InMemoryEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.evidence.sink import (
    EvidenceServiceSink,
    EvidenceWriteResult,
    EvidenceWriteStatus,
)
from app.db import create_evidence_engine
from app.db.base import Base
from app.services.evidence.persistence import SqlAlchemyEvidenceRepository

from intake_helpers import (
    SYNTHETIC_DOB,
    SYNTHETIC_EMAIL,
    SYNTHETIC_LICENCE,
    SYNTHETIC_STREET,
    SYNTHETIC_VIN,
    SENSITIVE_MARKERS as INTAKE_SENSITIVE_MARKERS,
)

# Sensitive synthetic values that must NEVER appear in evidence/audit/hashes/
# exports/API/URLs/logs.
APPLICANT_PHONE = "416-555-0199"
CLAIM_DETAILS = "rear-ended at Yonge & Queen in 2019"
QUOTE_REFERENCE = "Q-2026-8844-AB"

SENSITIVE_MARKERS = list(INTAKE_SENSITIVE_MARKERS) + [
    SYNTHETIC_EMAIL,
    APPLICANT_PHONE,
    CLAIM_DETAILS,
    QUOTE_REFERENCE,
    "M5V 1A1",  # applicant postal
    "Test Applicant",
]


# ---------------------------------------------------------------------------
# Privacy scanning (allowlist) - evidence CONTENT, not system-generated ids
# ---------------------------------------------------------------------------

# Opaque system-generated identifiers that are NEVER applicant-controlled.
# Random substrings inside them (e.g. "1990" inside a hex evidence_id) must
# never be treated as PII leakage, so privacy scans skip them entirely.
_OPAQUE_FIELDS: frozenset[str] = frozenset(
    {
        "evidence_id",
        "content_hash",
        "idempotency_key",
        "attempt_id",
        "parent_attempt_id",
        "quote_id",
        "quote_observation_id",
        "intake_session_id",
        "plan_id",
        "source_session_id",
        "consent_receipt_id",
        "private_reference_handle",
        "attachment_id",
        "sha256",
        "safe_reference",
        "registry_snapshot_ref",
        "audit_id",
        "request_id",
        "trace_id",
    }
)

# Top-level evidence fields that CAN carry user-controlled/applicant content.
# Allowlist: ONLY these are scanned (never whole-record JSON), so generated
# ids/hashes can never cause false positives. Payload is a typed safe model,
# but its content (messages, carrier labels, sanitized URLs) is still
# applicant-facing and must be checked.
_PII_CAPABLE_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "payload",
    "safe_url",
    "page_signature",
)


def _collect_content_strings(value: object, parts: list[str]) -> None:
    """Recursively collect scalar strings, skipping opaque/generated fields."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _OPAQUE_FIELDS:
                continue
            _collect_content_strings(child, parts)
    elif isinstance(value, list):
        for child in value:
            _collect_content_strings(child, parts)
    elif value is not None:
        parts.append(str(value))


def evidence_content_text(record) -> str:
    """Extract ONLY PII-capable content from one evidence record as text.

    Whole-record ``model_dump_json()`` scans are flaky: a short sensitive
    marker like "1990" can collide with a random substring inside a
    system-generated hex id (evidence_id / content_hash / idempotency_key),
    which is NOT applicant PII leakage. This allowlist helper scans only the
    fields that can actually carry user-controlled content (the typed payload
    plus sanitized URLs/signatures) and excludes every opaque system-generated
    identifier at any nesting depth.
    """
    data = record.model_dump(mode="json")
    parts: list[str] = []
    for key in _PII_CAPABLE_TOP_LEVEL_FIELDS:
        value = data.get(key)
        if value is not None:
            _collect_content_strings(value, parts)
    return "\n".join(parts)


def assert_evidence_privacy_safe(records, markers=None) -> None:
    """Assert no sensitive marker appears in PII-capable evidence content.

    Generated opaque identifiers (evidence_id, content_hash, idempotency_key,
    attempt/quote/session ids, opaque reference handles, attachment hashes)
    are excluded, so random id/hash substrings can never trip the check.
    """
    marker_list = list(markers if markers is not None else SENSITIVE_MARKERS)
    for record in records:
        content = evidence_content_text(record)
        for marker in marker_list:
            assert marker not in content, (
                f"sensitive marker {marker!r} leaked into PII-capable evidence "
                f"content of {type(record).__name__} "
                f"(event={getattr(record, 'event_type', '?')})"
            )


PLAN_ID = "plan-1"
ROUTE_ID = "mock-insurer"
REGISTRY_ID = "mock-insurer"
DISTINCT_RATE_SOURCE_ID = "RS-MOCK-INSURER"
ATTEMPT_ID = "att-100"
PARENT_ATTEMPT_ID = "att-90"


@dataclass
class EvidenceEnv:
    """Wired evidence environment (service + identifiers)."""

    service: EvidenceService
    intake_session_id: str = "intake-1"
    plan_id: str = PLAN_ID
    planned_route_id: str = ROUTE_ID
    registry_id: str = REGISTRY_ID
    distinct_rate_source_id: str = DISTINCT_RATE_SOURCE_ID
    attempt_id: str = ATTEMPT_ID
    parent_attempt_id: str = PARENT_ATTEMPT_ID
    repo: object = field(default=None, repr=False)

    def ids(self, **overrides: str) -> dict:
        base = {
            "plan_id": self.plan_id,
            "planned_route_id": self.planned_route_id,
            "registry_id": self.registry_id,
            "distinct_rate_source_id": self.distinct_rate_source_id,
            "attempt_id": self.attempt_id,
        }
        base.update({k: v for k, v in overrides.items() if v is not None})
        return base


def make_evidence_env(intake_session_id: str = "intake-1") -> EvidenceEnv:
    """In-memory (hermetic, ephemeral) evidence environment."""
    repo = InMemoryEvidenceRepository()
    service = EvidenceService(repo)
    return EvidenceEnv(service=service, intake_session_id=intake_session_id, repo=repo)


def make_sink_env(intake_session_id: str = "intake-1") -> tuple[EvidenceEnv, EvidenceServiceSink]:
    """In-memory evidence environment with a synchronous service sink."""
    env = make_evidence_env(intake_session_id)
    return env, EvidenceServiceSink(env.service)


class FailingEvidenceSink:
    """Test double: records calls but every write FAILS (never persists).

    Mirrors the sink contract so engines exercise their persistence-failure
    path deterministically (result = persistence_failed, no exception).
    """

    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def record(self, intake_session_id: str, draft) -> EvidenceWriteResult:
        self.calls.append(("record", intake_session_id, draft))
        return EvidenceWriteResult(
            EvidenceWriteStatus.PERSISTENCE_FAILED, error_category="TestFailure"
        )

    def record_quote(self, intake_session_id: str, quote) -> EvidenceWriteResult:
        self.calls.append(("quote", intake_session_id, quote))
        return EvidenceWriteResult(
            EvidenceWriteStatus.PERSISTENCE_FAILED, error_category="TestFailure"
        )

    def record_audit(
        self, intake_session_id: str, *, event_name, actor="system", safe_metadata=None
    ) -> EvidenceWriteResult:
        self.calls.append(("audit", intake_session_id, event_name))
        return EvidenceWriteResult(
            EvidenceWriteStatus.PERSISTENCE_FAILED, error_category="TestFailure"
        )

    def evidence_status(self) -> str:
        return EvidenceWriteStatus.PERSISTENCE_FAILED.value


async def make_sqlite_evidence_env(
    tmp_path: Path, intake_session_id: str = "intake-1"
) -> EvidenceEnv:
    """SQLite-backed evidence environment (same SQLAlchemy models as Postgres)."""
    db_path = (tmp_path / "evidence.db").as_posix()
    engine = create_evidence_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    repo = SqlAlchemyEvidenceRepository(engine)
    service = EvidenceService(repo)
    return EvidenceEnv(service=service, intake_session_id=intake_session_id, repo=engine)


# ---------------------------------------------------------------------------
# Browser observation builders (constructed directly - no real browser)
# ---------------------------------------------------------------------------


def page_observation(
    page_signature: str = "mock:auto:quote:page-a",
    url: str = "http://127.0.0.1:8765/page-a",
    bot_protection: bool = False,
) -> BrowserObservation:
    return BrowserObservation(
        observation_type=BrowserObservationType.PAGE_LOADED,
        page_index=0,
        page_signature=page_signature,
        url=url,
        message="page loaded",
    )


def quote_observation(
    annual: Optional[float] = 1234.56,
    monthly: Optional[float] = 120.0,
    currency: str = "CAD",
    firm: bool = True,
    reference_present: bool = True,
    observed_at: Optional[dt.datetime] = None,
    private_handle: str = "opaque-ref-hash",
    url: str = "http://127.0.0.1:8765/page-a?postal=M5V1A1&token=SECRET",
) -> BrowserObservation:
    raw = RawQuoteObservation(
        registry_id=REGISTRY_ID,
        observed_at=observed_at or dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
        source_url=url,
        annual_amount_raw=f"${annual:,.2f}" if annual is not None else None,
        annual_amount_parsed=annual,
        monthly_amount_raw=f"${monthly:,.2f}" if monthly is not None else None,
        monthly_amount_parsed=monthly,
        currency=currency,
        coverage_observations=["liability-1m"],
        reference_present=reference_present,
        private_reference_handle=private_handle if reference_present else None,
        is_firm_quote=firm,
    )
    return BrowserObservation(
        observation_type=BrowserObservationType.QUOTE_DETECTED,
        page_index=2,
        page_signature="mock:auto:quote:quote-page",
        url="http://127.0.0.1:8765/quote-page",
        message="quote detected",
        quote=BrowserQuoteObservation(quote_present=True, reference_present=reference_present, raw=raw),
    )


def access_control_observation() -> BrowserObservation:
    return BrowserObservation(
        observation_type=BrowserObservationType.ACCESS_CONTROL_DETECTED,
        page_index=1,
        page_signature="mock:auto:quote:gate",
        url="http://127.0.0.1:8765/gate",
        message="access control challenge present",
    )


def _utc(year: int = 2026, month: int = 1, day: int = 1) -> dt.datetime:
    return dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
