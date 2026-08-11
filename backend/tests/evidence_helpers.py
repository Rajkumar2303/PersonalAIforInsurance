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
