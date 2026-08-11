"""Shared helpers for Issue #11 quote-normalization tests.

Hermetic by design: no real insurer/telephony/LLM calls, no LangSmith uploads,
no applicant data. Normalization is exercised through the
``QuoteNormalizationService`` against an in-memory evidence repository (unit
tests) or SQLite-backed repositories (persistence/lineage/API tests).

Sensitive markers assert that licence/VIN/DOB/street/email/phone/claims/raw
quote references never leak into normalized quotes, hashes, API views, or
exports (same discipline as Issue #10 evidence tests).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from app.models.browser.observation import (
    BrowserObservation,
    BrowserObservationType,
    BrowserQuoteObservation,
    RawQuoteObservation,
)
from app.models.normalization import NormalizedQuote
from app.models.recovery import SourceChannel
from app.services.evidence.repository import InMemoryEvidenceRepository
from app.services.evidence.service import EvidenceService
from app.services.normalization.config import CoverageMappingRegistry
from app.services.normalization.repository import InMemoryNormalizationRepository
from app.services.normalization.service import QuoteNormalizationService

from evidence_helpers import (
    REGISTRY_ID,
    SENSITIVE_MARKERS,
    make_evidence_env,
)

PLAN_ID = "plan-1"
ROUTE_ID = "mock-insurer"
DISTINCT_RATE_SOURCE_ID = "RS-MOCK-INSURER"
ATTEMPT_ID = "att-100"
AGGREGATOR_REGISTRY_ID = "mock-aggregator"


@dataclass
class NormalizationEnv:
    """Wired normalization environment (evidence service + normalizer)."""

    evidence: EvidenceService
    normalization: QuoteNormalizationService
    intake_session_id: str = "intake-1"
    plan_id: str = PLAN_ID
    planned_route_id: str = ROUTE_ID
    registry_id: str = REGISTRY_ID
    distinct_rate_source_id: str = DISTINCT_RATE_SOURCE_ID
    attempt_id: str = ATTEMPT_ID
    source_evidence_ids: Optional[list[str]] = None

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


def make_normalization_env(
    intake_session_id: str = "intake-1",
    data_dir: Optional[Path] = None,
) -> NormalizationEnv:
    """In-memory (hermetic) normalization environment."""
    evidence_env = make_evidence_env(intake_session_id)
    registry = CoverageMappingRegistry(data_dir) if data_dir else None
    service = QuoteNormalizationService(
        evidence_env.service,
        InMemoryNormalizationRepository(),
        registry=registry,
    )
    return NormalizationEnv(
        evidence=evidence_env.service,
        normalization=service,
        intake_session_id=intake_session_id,
    )


def make_browser_quote(
    *,
    annual: Optional[Decimal] = Decimal("1234.56"),
    monthly: Optional[Decimal] = None,
    currency: str = "CAD",
    firm: bool = True,
    coverage: Optional[list[str]] = None,
    discounts: Optional[list[str]] = None,
    observed_at: Optional[dt.datetime] = None,
    **id_overrides: str,
) -> BrowserObservation:
    """Construct a QUOTE_DETECTED browser observation (no real browser)."""
    raw = RawQuoteObservation(
        registry_id=id_overrides.get("registry_id", REGISTRY_ID),
        observed_at=observed_at or dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
        source_url="http://127.0.0.1:8765/quote-page",
        annual_amount_raw=f"${annual:,.2f}" if annual is not None else None,
        annual_amount_parsed=float(annual) if annual is not None else None,
        annual_amount_decimal=annual,
        monthly_amount_raw=f"${monthly:,.2f}" if monthly is not None else None,
        monthly_amount_parsed=float(monthly) if monthly is not None else None,
        monthly_amount_decimal=monthly,
        currency=currency,
        coverage_observations=list(coverage or []),
        discount_observations=list(discounts or []),
        reference_present=False,
        is_firm_quote=firm,
    )
    return BrowserObservation(
        observation_type=BrowserObservationType.QUOTE_DETECTED,
        page_index=2,
        page_signature="mock:auto:quote:quote-page",
        url="http://127.0.0.1:8765/quote-page",
        message="quote detected",
        quote=BrowserQuoteObservation(quote_present=True, reference_present=False, raw=raw),
    )


async def record_and_normalize(
    env: NormalizationEnv,
    observation: BrowserObservation,
    *,
    source_evidence_ids: Optional[list[str]] = None,
    **id_overrides: str,
) -> tuple[object, NormalizedQuote]:
    """Record a browser observation into evidence, then normalize it."""
    from app.services.evidence.ingest import quote_from_browser_observation

    ids = env.ids(**id_overrides)
    quote = quote_from_browser_observation(
        env.intake_session_id,
        observation,
        plan_id=ids["plan_id"],
        planned_route_id=ids["planned_route_id"],
        registry_id=ids["registry_id"],
        distinct_rate_source_id=ids["distinct_rate_source_id"],
        attempt_id=ids["attempt_id"],
    )
    stored = await env.evidence.record_quote_observation(env.intake_session_id, quote)
    normalized = await env.normalization.normalize(
        env.intake_session_id,
        stored.quote_id,
        source_evidence_record_ids=source_evidence_ids,
    )
    return stored, normalized


async def record_voice_quote(
    env: NormalizationEnv,
    *,
    annual: Optional[Decimal] = None,
    monthly: Optional[Decimal] = None,
    attempt_id: str = "voice-attempt-1",
) -> object:
    """Record a voice-originated quote (no structured premium by default)."""
    from app.services.evidence.ingest import voice_quote

    quote = voice_quote(
        intake_session_id=env.intake_session_id,
        voice_session_id="voice-session-1",
        plan_id=env.plan_id,
        registry_id=env.registry_id,
        distinct_rate_source_id=env.distinct_rate_source_id,
        planned_route_id=env.planned_route_id,
        attempt_id=attempt_id,
        annual_premium=annual,
        monthly_premium=monthly,
        reference_present=False,
    )
    return await env.evidence.record_quote_observation(env.intake_session_id, quote)


def assert_no_sensitive_markers(obj: object) -> None:
    """Assert that sensitive synthetic values never appear in a serialized obj."""
    import json

    if isinstance(obj, dict):
        text = json.dumps(obj, default=str)
    elif hasattr(obj, "model_dump"):
        text = json.dumps(obj.model_dump(mode="json"), default=str)
    else:
        text = str(obj)
    for marker in SENSITIVE_MARKERS:
        assert marker.lower() not in text.lower(), f"sensitive marker leaked: {marker}"


async def make_sqlite_normalization_env(
    tmp_path: Path, intake_session_id: str = "intake-1"
) -> NormalizationEnv:
    """SQLite-backed normalization environment (same models as Postgres)."""
    from app.db import create_evidence_engine
    from app.db.base import Base
    from app.services.evidence.persistence import SqlAlchemyEvidenceRepository
    from app.services.normalization.repository import SqlAlchemyNormalizationRepository

    db_path = (tmp_path / "normalization.db").as_posix()
    engine = create_evidence_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    evidence = EvidenceService(SqlAlchemyEvidenceRepository(engine))
    normalization = QuoteNormalizationService(
        evidence,
        SqlAlchemyNormalizationRepository(engine),
    )
    return NormalizationEnv(
        evidence=evidence,
        normalization=normalization,
        intake_session_id=intake_session_id,
    )


def source_channel(attempt_id: str) -> SourceChannel:
    if attempt_id.startswith("voice"):
        return SourceChannel.VOICE
    if attempt_id.startswith("phone"):
        return SourceChannel.PHONE
    return SourceChannel.BROWSER
