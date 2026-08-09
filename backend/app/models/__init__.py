"""Pydantic v2 models.

Issue #1 added the demo workflow models; Issue #2 adds the canonical
insurance intake schema (``app/models/insurance/``) with AUTO fully
implemented and HOME/TENANT/LIFE/TRAVEL/OTHER recognized but unsupported.
Future milestones add: MarketRegistryEntry, RoutePlan, QuoteAttempt,
EvidenceRecord, NormalizedQuote, CoverageLedger, ComparisonResult,
WorkflowState.
"""

from __future__ import annotations

from .dedup import (
    Confidence,
    DeduplicationDecision,
    DeduplicationStatus,
    DeduplicatedMarket,
    DistinctRateSource,
    DuplicateCandidate,
    DuplicateGroup,
    ReasonCode,
)
from .demo import DemoWorkflowRequest, DemoWorkflowResponse
from .insurance import (
    AddressInformation,
    ApplicantIdentity,
    ApplicantInformation,
    AutoInsuranceProfile,
    ConsentState,
    ContactInformation,
    InsuranceProfile,
    InsuranceType,
    SensitiveBaseModel,
)
from .registry import (
    DistributionType,
    MarketRegistryEntry,
    MarketRequirement,
    ProductScope,
    RegistryStatus,
)

__all__ = [
    "DemoWorkflowRequest",
    "DemoWorkflowResponse",
    "InsuranceProfile",
    "AutoInsuranceProfile",
    "ConsentState",
    "ApplicantInformation",
    "ApplicantIdentity",
    "ContactInformation",
    "AddressInformation",
    "InsuranceType",
    "SensitiveBaseModel",
    "MarketRegistryEntry",
    "DistributionType",
    "ProductScope",
    "RegistryStatus",
    "MarketRequirement",
    "DistinctRateSource",
    "DeduplicationStatus",
    "DeduplicationDecision",
    "DuplicateCandidate",
    "DuplicateGroup",
    "DeduplicatedMarket",
    "Confidence",
    "ReasonCode",
]
