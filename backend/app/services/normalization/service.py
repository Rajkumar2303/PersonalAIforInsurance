"""Quote normalization service (Issue #11, Prompt 1).

Consumes Issue #10 durable ``QuoteObservation`` rows and produces canonical,
provider-independent ``NormalizedQuote`` objects WITHOUT deciding
comparability/ranking (Issue #12 owns that). Deterministic domain logic only:
no LLM, no fuzzy matching, no ``if registry_id`` branching, no currency
conversion (hard-CAD).

Idempotency: unique on ``(source_quote_observation_id,
normalization_rule_version)``; re-running returns the same normalized quote.
Raw evidence is NEVER mutated - normalization reads evidence and writes only
to the normalization store.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from functools import lru_cache
from typing import Optional

from ...core.config import get_settings
from ...models.evidence import QuoteObservation
from ...models.normalization import (
    CoverageLedger,
    NormalizationStatus,
    NormalizedQuote,
    PremiumNormalized,
)
from ...models.recovery import SourceChannel
from ..evidence.hashing import canonical_json, sha256_hex
from ..evidence.service import EvidenceService
from .config import CoverageMappingRegistry, get_coverage_mapping_registry
from .coverage import CoverageNormalizer
from .money import PremiumNormalizer
from .repository import NormalizationRepository

logger = logging.getLogger(__name__)

# Operational fields excluded from the normalized-quote content hash (mirrors
# the Issue #10 hashing discipline: hash the semantic contents, not identity/
# bookkeeping).
NORMALIZED_QUOTE_EXCLUDED_FIELDS = {
    "content_hash",
    "idempotency_key",
    "created_at",
    "normalized_quote_id",
}


def normalized_quote_content_hash(quote: NormalizedQuote) -> str:
    """Deterministic SHA-256 over the semantic contents of a NormalizedQuote.

    Uses mode="python" so datetimes/Decimals stay typed and the canonical JSON
    serializer (evidence hashing) can normalize aware datetimes to UTC-naive -
    making an in-memory (aware) object hash identically to a SQLite-reloaded
    (naive UTC) object.
    """
    data = quote.model_dump(exclude=NORMALIZED_QUOTE_EXCLUDED_FIELDS)
    return sha256_hex(canonical_json(data))


def normalized_idempotency_key(source_quote_observation_id: str, rule_version: str) -> str:
    return f"norm:{source_quote_observation_id}:{rule_version}"


class QuoteNormalizationService:
    """Deterministic normalization of durable quote observations."""

    def __init__(
        self,
        evidence_service: EvidenceService,
        repository: NormalizationRepository,
        registry: Optional[CoverageMappingRegistry] = None,
        premium_normalizer: Optional[PremiumNormalizer] = None,
        coverage_normalizer: Optional[CoverageNormalizer] = None,
        rule_version: Optional[str] = None,
    ) -> None:
        self._evidence = evidence_service
        self._repository = repository
        self._registry = registry or get_coverage_mapping_registry()
        self._premium = premium_normalizer or PremiumNormalizer(currency=self._registry.currency)
        self._coverage = coverage_normalizer or CoverageNormalizer(self._registry)
        self.rule_version = rule_version or self._registry.rule_version

    # --- public API --------------------------------------------------

    async def normalize(
        self,
        intake_session_id: str,
        source_quote_observation_id: str,
        source_evidence_record_ids: Optional[list[str]] = None,
    ) -> NormalizedQuote:
        """Normalize one source quote observation (idempotent per rule)."""
        source = await self._evidence.get_quote_observation(
            intake_session_id, source_quote_observation_id
        )
        if source is None:
            raise QuoteNormalizationError(
                f"source quote observation not found: {source_quote_observation_id}"
            )

        premium = self._premium.normalize(
            annual_premium=source.annual_premium,
            monthly_premium=source.monthly_premium,
            currency=source.currency,
        )
        ledger = self._coverage.normalize(
            coverage_observations=source.coverage_observations,
            discount_observations=source.discount_observations,
            source_evidence_ids=list(source_evidence_record_ids or []),
        )
        status = _determine_status(premium, ledger, source)

        now = dt.datetime.now(dt.timezone.utc)
        quote = NormalizedQuote(
            normalized_quote_id=uuid.uuid4().hex,
            intake_session_id=source.intake_session_id,
            plan_id=source.plan_id,
            planned_route_id=source.planned_route_id,
            registry_id=source.registry_id,
            distinct_rate_source_id=source.distinct_rate_source_id,
            aggregator_registry_id=source.aggregator_registry_id,
            presented_carrier=source.presented_carrier,
            attempt_id=source.attempt_id,
            parent_attempt_id=source.parent_attempt_id,
            source_quote_observation_id=source.quote_id,
            source_channel=_source_channel_from(source),
            firm_vs_estimate=source.firm_vs_estimate,
            premium=premium,
            coverage_ledger=ledger,
            normalization_status=status,
            normalization_rule_version=self.rule_version,
            normalized_at=now,
            source_evidence_record_ids=list(source_evidence_record_ids or []),
            content_hash="",
            idempotency_key=normalized_idempotency_key(source.quote_id, self.rule_version),
            created_at=now,
        )
        quote = quote.model_copy(
            update={"content_hash": normalized_quote_content_hash(quote)}
        )
        stored = await self._repository.save_normalized_quote(quote)
        logger.info(
            "quote normalized",
            extra={
                "workflow": "normalization",
                "workflow_stage": "normalize",
                "status": "ok",
                "normalization_status": stored.normalization_status.value,
                "rule_version": self.rule_version,
                "registry_id": stored.registry_id,
                "distinct_rate_source_id": stored.distinct_rate_source_id,
                "source_quote_observation_id": stored.source_quote_observation_id,
                "attempt_id": stored.attempt_id,
            },
        )
        return stored

    async def get(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> Optional[NormalizedQuote]:
        return await self._repository.get(intake_session_id, normalized_quote_id)

    async def list_by_intake(self, intake_session_id: str) -> list[NormalizedQuote]:
        return await self._repository.list_by_intake(intake_session_id)

    async def list_by_plan(
        self, intake_session_id: str, plan_id: str
    ) -> list[NormalizedQuote]:
        return await self._repository.list_by_plan(intake_session_id, plan_id)

    async def list_by_route(
        self, intake_session_id: str, planned_route_id: str
    ) -> list[NormalizedQuote]:
        return await self._repository.list_by_route(intake_session_id, planned_route_id)

    async def list_by_attempt(
        self, intake_session_id: str, attempt_id: str
    ) -> list[NormalizedQuote]:
        return await self._repository.list_by_attempt(intake_session_id, attempt_id)

    async def verify_integrity(
        self, intake_session_id: str, normalized_quote_id: str
    ) -> bool:
        return await self._repository.verify_integrity(intake_session_id, normalized_quote_id)

    async def delete_by_intake_session(self, intake_session_id: str) -> int:
        return await self._repository.delete_by_intake_session(intake_session_id)


class QuoteNormalizationError(RuntimeError):
    """Raised when a source quote cannot be normalized."""


def _determine_status(
    premium: PremiumNormalized,
    ledger: CoverageLedger,
    source: QuoteObservation,
) -> NormalizationStatus:
    """Deterministic status - never assigns comparable/non_comparable."""
    has_premium = premium.normalized_annual_amount is not None
    has_mapped_coverage = ledger.mapped_count > 0

    if not has_premium and not has_mapped_coverage:
        return NormalizationStatus.INSUFFICIENT_EVIDENCE
    if has_premium and has_mapped_coverage:
        return NormalizationStatus.NORMALIZED
    return NormalizationStatus.PARTIALLY_NORMALIZED


def _source_channel_from(source: QuoteObservation) -> SourceChannel:
    if source.attempt_id and source.attempt_id.startswith("voice"):
        return SourceChannel.VOICE
    if source.attempt_id and source.attempt_id.startswith("phone"):
        return SourceChannel.PHONE
    if source.attempt_id:
        return SourceChannel.BROWSER
    return SourceChannel.MANUAL


# ---------------------------------------------------------------------------
# API-safe view builders (mirrors the evidence service view pattern)
# ---------------------------------------------------------------------------


def _premium_view(premium: PremiumNormalized) -> "PremiumView":
    from ...models.normalization import PremiumView

    return PremiumView(
        provider_presented_amount=str(premium.provider_presented_amount)
        if premium.provider_presented_amount is not None
        else None,
        provider_presented_frequency=premium.provider_presented_frequency,
        normalized_annual_amount=str(premium.normalized_annual_amount)
        if premium.normalized_annual_amount is not None
        else None,
        currency=premium.currency,
        annualized=premium.annualized,
        derivation=premium.derivation.value,
        derivation_rule=premium.derivation_rule,
    )


def _item_view(item: "CoverageLedgerItem") -> "CoverageLedgerItemView":
    from ...models.normalization import CoverageLedgerItemView

    return CoverageLedgerItemView(
        item_key=item.item_key.value,
        state=item.state.value,
        value=item.value.model_dump(mode="json") if item.value else None,
        provenance=item.provenance.value,
        raw_labels=list(item.raw_labels),
        source_evidence_ids=list(item.source_evidence_ids),
    )


def _ledger_view(ledger: CoverageLedger) -> "CoverageLedgerView":
    from ...models.normalization import CoverageLedgerView

    return CoverageLedgerView(
        items=[_item_view(i) for i in ledger.ordered_items()],
        unmapped_coverage=[
            u.model_dump(mode="json") for u in ledger.unmapped_coverage
        ],
    )


def _quote_view(quote: NormalizedQuote) -> "NormalizedQuoteView":
    from ...models.normalization import NormalizedQuoteView

    return NormalizedQuoteView(
        normalized_quote_id=quote.normalized_quote_id,
        intake_session_id=quote.intake_session_id,
        plan_id=quote.plan_id,
        planned_route_id=quote.planned_route_id,
        registry_id=quote.registry_id,
        distinct_rate_source_id=quote.distinct_rate_source_id,
        aggregator_registry_id=quote.aggregator_registry_id,
        presented_carrier=quote.presented_carrier,
        attempt_id=quote.attempt_id,
        parent_attempt_id=quote.parent_attempt_id,
        source_quote_observation_id=quote.source_quote_observation_id,
        source_channel=quote.source_channel.value,
        firm_vs_estimate=quote.firm_vs_estimate,
        premium=_premium_view(quote.premium),
        coverage_ledger=_ledger_view(quote.coverage_ledger),
        normalization_status=quote.normalization_status.value,
        normalization_rule_version=quote.normalization_rule_version,
        normalized_at=quote.normalized_at,
        source_evidence_record_ids=list(quote.source_evidence_record_ids),
        content_hash=quote.content_hash,
    )


def _export_view(
    intake_session_id: str, quotes: list[NormalizedQuote], rule_version: str
) -> "NormalizedExportView":
    from ...models.normalization import NormalizedExportView

    attempts = sorted({q.attempt_id for q in quotes if q.attempt_id})
    routes = sorted({q.planned_route_id for q in quotes if q.planned_route_id})
    return NormalizedExportView(
        intake_session_id=intake_session_id,
        exported_at=dt.datetime.now(dt.timezone.utc),
        normalized_quote_count=len(quotes),
        distinct_attempts=attempts,
        distinct_routes=routes,
        normalization_rule_version=rule_version,
        quotes=[_quote_view(q) for q in quotes],
    )


@lru_cache(maxsize=1)
def get_quote_normalization_service() -> QuoteNormalizationService:
    """App singleton - in-memory normalization repo (hermetic default)."""
    from ..evidence import get_evidence_service
    from .repository import InMemoryNormalizationRepository

    settings = get_settings()
    if settings.evidence_repository_backend == "postgres":
        from ...db import create_evidence_engine, default_evidence_database_url
        from ..evidence import get_evidence_service

        url = default_evidence_database_url()
        if url:
            from .repository import SqlAlchemyNormalizationRepository

            engine = create_evidence_engine(url)
            repo: NormalizationRepository = SqlAlchemyNormalizationRepository(engine)
            return QuoteNormalizationService(get_evidence_service(), repo)

    from ..evidence import get_evidence_service

    repo = InMemoryNormalizationRepository()
    return QuoteNormalizationService(get_evidence_service(), repo)
