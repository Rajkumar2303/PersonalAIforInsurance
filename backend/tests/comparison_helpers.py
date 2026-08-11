"""Shared helpers for Issue #12 lite-comparison tests.

Builds ``NormalizedQuote`` objects directly (no evidence/DB) so the comparison
logic is exercised in isolation. Hermetic: no LLM, no LangSmith, no applicant
data, no insurer calls.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from app.models.normalization import (
    CoverageItemKey,
    CoverageItemState,
    CoverageLedger,
    CoverageLedgerItem,
    CoverageProvenance,
    MoneyCoverageValue,
    NormalizationStatus,
    NormalizedQuote,
    PremiumDerivation,
    PremiumNormalized,
)
from app.models.recovery import SourceChannel

NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

DEFAULT_TPL = Decimal("2000000")
DEFAULT_COLLISION = Decimal("1000")
DEFAULT_COMPREHENSIVE = Decimal("500")


def make_quote(
    *,
    normalized_quote_id: str = "nq-1",
    intake_session_id: str = "intake-1",
    plan_id: str = "plan-1",
    planned_route_id: str = "route-1",
    registry_id: str = "mock-a",
    presented_carrier: str = "Carrier A",
    distinct_rate_source_id: str = "RS-A",
    aggregator_registry_id: Optional[str] = None,
    source_quote_observation_id: str = "q-1",
    annual_premium: Optional[Decimal] = Decimal("2000.00"),
    firm_vs_estimate: str = "firm",
    normalization_status: NormalizationStatus = NormalizationStatus.NORMALIZED,
    tpl: Optional[Decimal] = DEFAULT_TPL,
    collision: Optional[Decimal] = DEFAULT_COLLISION,
    comprehensive: Optional[Decimal] = DEFAULT_COMPREHENSIVE,
    unknown_keys: frozenset = frozenset(),
) -> NormalizedQuote:
    """Build a NormalizedQuote with the essential coverage ledger."""
    ledger = CoverageLedger()
    for key, amount in (
        (CoverageItemKey.THIRD_PARTY_LIABILITY, tpl),
        (CoverageItemKey.COLLISION, collision),
        (CoverageItemKey.COMPREHENSIVE, comprehensive),
    ):
        if key in unknown_keys:
            ledger.set_item(
                CoverageLedgerItem(
                    item_key=key,
                    state=CoverageItemState.UNKNOWN,
                    value=None,
                    provenance=CoverageProvenance.UNKNOWN,
                    raw_labels=[],
                    source_evidence_ids=[],
                )
            )
        elif amount is not None:
            ledger.set_item(
                CoverageLedgerItem(
                    item_key=key,
                    state=CoverageItemState.INCLUDED,
                    value=MoneyCoverageValue(amount=amount),
                    provenance=CoverageProvenance.MAPPED_ALIAS,
                    raw_labels=[],
                    source_evidence_ids=[],
                )
            )

    premium = PremiumNormalized(
        provider_presented_amount=annual_premium,
        provider_presented_frequency="annual" if annual_premium is not None else None,
        normalized_annual_amount=annual_premium,
        currency="CAD",
        annualized=False,
        derivation=PremiumDerivation.DIRECTLY_QUOTED if annual_premium is not None else PremiumDerivation.UNKNOWN,
    )

    return NormalizedQuote(
        normalized_quote_id=normalized_quote_id,
        intake_session_id=intake_session_id,
        plan_id=plan_id,
        planned_route_id=planned_route_id,
        registry_id=registry_id,
        distinct_rate_source_id=distinct_rate_source_id,
        aggregator_registry_id=aggregator_registry_id,
        presented_carrier=presented_carrier,
        attempt_id="att-1",
        source_quote_observation_id=source_quote_observation_id,
        source_channel=SourceChannel.BROWSER,
        firm_vs_estimate=firm_vs_estimate,
        premium=premium,
        coverage_ledger=ledger,
        normalization_status=normalization_status,
        normalization_rule_version="1",
        normalized_at=NOW,
        content_hash="",
        idempotency_key="",
        created_at=NOW,
    )
