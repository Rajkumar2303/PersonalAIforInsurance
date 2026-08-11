"""Quote normalization & coverage ledger domain models (Issue #11, Prompt 1).

Turns provider-specific/raw quote observations into a consistent, canonical
quote representation WITHOUT deciding comparability/ranking (Issue #12 owns
that). Everything here is SAFE metadata + Decimal money - never applicant PII,
never raw quote references, never raw DOM/page text.

Guarantees:
- ``unknown`` coverage is FIRST-CLASS and is NEVER collapsed into ``excluded``.
- Partial normalization keeps all known data (a quote with one unknown
  component does not fail).
- Firm vs estimate is preserved; estimates are never promoted.
- Provider wording is mapped through an explicit, data-driven alias registry -
  no fuzzy matching, no ``if registry_id`` branching.
- ``quoted_comparable`` / ``quoted_non_comparable`` are never assigned here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import ConfigDict, Field, TypeAdapter

from .insurance.base import SensitiveBaseModel
from .recovery import SourceChannel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CoverageItemState(StrEnum):
    """Canonical state of one coverage component (never collapses unknown)."""

    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    NOT_OFFERED = "not_offered"
    OPTIONAL_NOT_SELECTED = "optional_not_selected"


class CoverageProvenance(StrEnum):
    """Field-level provenance (NOT Issue #12's overall confidence)."""

    DIRECTLY_OBSERVED = "directly_observed"
    EXPLICITLY_STATED = "explicitly_stated"
    DERIVED = "derived"
    MAPPED_ALIAS = "mapped_alias"
    UNKNOWN = "unknown"


class NormalizationStatus(StrEnum):
    """Safe normalization lifecycle. Never uses comparable/non_comparable."""

    PENDING = "pending"
    NORMALIZED = "normalized"
    PARTIALLY_NORMALIZED = "partially_normalized"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_SOURCE = "invalid_source"
    NORMALIZATION_FAILED = "normalization_failed"


class PremiumDerivation(StrEnum):
    DIRECTLY_QUOTED = "directly_quoted"
    DERIVED_ANNUALIZED = "derived_annualized"
    UNKNOWN = "unknown"


class CoverageItemKey(StrEnum):
    """Canonical Ontario auto coverage components (extensible by enum member).

    Intentionally not one DB column per endorsement: coverage items are stored
    as typed rows keyed by this enum, so adding a new canonical key is an enum
    + config change, never a brittle migration.
    """

    THIRD_PARTY_LIABILITY = "third_party_liability"
    ACCIDENT_BENEFITS = "accident_benefits"
    DCPD = "dcpd"
    UNINSURED_AUTOMOBILE = "uninsured_automobile"
    COLLISION = "collision"
    COMPREHENSIVE = "comprehensive"
    SPECIFIED_PERILS = "specified_perils"
    ALL_PERILS = "all_perils"
    OPCF_44R_FAMILY_PROTECTION = "opcf_44r_family_protection"
    OPCF_20 = "opcf_20"
    OPCF_27 = "opcf_27"
    OPCF_43 = "opcf_43"
    TELEMATICS = "telematics"
    BUNDLE_DISCOUNT = "bundle_discount"
    MULTI_VEHICLE_DISCOUNT = "multi_vehicle_discount"
    WINTER_TIRES_DISCOUNT = "winter_tires_discount"
    CLAIMS_FREE_DISCOUNT = "claims_free_discount"
    CONVICTION_FREE_DISCOUNT = "conviction_free_discount"
    INSTALLMENT_FEE = "installment_fee"
    SERVICE_FEE = "service_fee"
    TAXES = "taxes"


# ---------------------------------------------------------------------------
# Typed coverage values (never arbitrary dicts)
# ---------------------------------------------------------------------------


class CoverageValueBase(SensitiveBaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str


class MoneyCoverageValue(CoverageValueBase):
    """A money-like coverage value (limit / deductible / fee) in Decimal."""

    kind: Literal["money"] = "money"
    amount: Decimal
    currency: str = "CAD"


class BooleanCoverageValue(CoverageValueBase):
    kind: Literal["boolean"] = "boolean"
    present: bool = True


class EndorsementCoverageValue(CoverageValueBase):
    kind: Literal["endorsement"] = "endorsement"
    code: str


CoverageValue = Annotated[
    Union[MoneyCoverageValue, BooleanCoverageValue, EndorsementCoverageValue],
    Field(discriminator="kind"),
]

COVERAGE_VALUE_ADAPTER: TypeAdapter[CoverageValue] = TypeAdapter(CoverageValue)


def validate_coverage_value(data: Any) -> CoverageValue:
    return COVERAGE_VALUE_ADAPTER.validate_python(data)


# ---------------------------------------------------------------------------
# Coverage ledger
# ---------------------------------------------------------------------------


class CoverageLedgerItem(SensitiveBaseModel):
    """One canonical coverage component with state, value, provenance, lineage."""

    model_config = ConfigDict(extra="forbid")

    item_key: CoverageItemKey
    state: CoverageItemState = CoverageItemState.UNKNOWN
    value: Optional[CoverageValue] = None
    provenance: CoverageProvenance = CoverageProvenance.UNKNOWN
    raw_labels: list[str] = Field(default_factory=list)  # safe provider labels
    source_evidence_ids: list[str] = Field(default_factory=list)  # safe refs only


class UnmappedCoverageObservation(SensitiveBaseModel):
    """A safe provider coverage label that had no canonical mapping.

    Preserved (never guessed, never discarded). Public provider wording only.
    """

    model_config = ConfigDict(extra="forbid")

    provider_label: str  # public label; never PII, never DOM dump
    source_evidence_ids: list[str] = Field(default_factory=list)


class CoverageLedger(SensitiveBaseModel):
    """Deterministic canonical coverage ledger keyed by CoverageItemKey."""

    model_config = ConfigDict(extra="forbid")

    items: dict[str, CoverageLedgerItem] = Field(default_factory=dict)
    unmapped_coverage: list[UnmappedCoverageObservation] = Field(default_factory=list)

    def get(self, key: CoverageItemKey) -> Optional[CoverageLedgerItem]:
        return self.items.get(key.value)

    def set_item(self, item: CoverageLedgerItem) -> None:
        self.items[item.item_key.value] = item

    def ordered_items(self) -> list[CoverageLedgerItem]:
        """Deterministic ordering by canonical key value."""
        return [self.items[k] for k in sorted(self.items)]

    @property
    def unknown_count(self) -> int:
        return sum(1 for it in self.items.values() if it.state is CoverageItemState.UNKNOWN)

    @property
    def mapped_count(self) -> int:
        return len(self.items)


# ---------------------------------------------------------------------------
# Premium
# ---------------------------------------------------------------------------


class PremiumComponent(SensitiveBaseModel):
    """One preserved premium-related component (fee / installment / tax)."""

    model_config = ConfigDict(extra="forbid")

    kind: str  # premium | installment_fee | service_fee | tax
    amount: Decimal
    currency: str = "CAD"
    provenance: CoverageProvenance = CoverageProvenance.DIRECTLY_OBSERVED


class PremiumNormalized(SensitiveBaseModel):
    """Provider-presented + canonical premium representation (Decimal only)."""

    model_config = ConfigDict(extra="forbid")

    provider_presented_amount: Optional[Decimal] = None
    provider_presented_frequency: Optional[str] = None  # annual | monthly | other
    normalized_annual_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    annualized: bool = False
    derivation: PremiumDerivation = PremiumDerivation.UNKNOWN
    derivation_rule: Optional[str] = None
    components: list[PremiumComponent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalized quote
# ---------------------------------------------------------------------------


class NormalizedQuote(SensitiveBaseModel):
    """Provider-independent canonical quote (consumes Issue #10 evidence)."""

    model_config = ConfigDict(extra="forbid")

    normalized_quote_id: str
    intake_session_id: str
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    aggregator_registry_id: Optional[str] = None
    presented_carrier: Optional[str] = None
    attempt_id: Optional[str] = None
    parent_attempt_id: Optional[str] = None
    source_quote_observation_id: str  # Issue #10 QuoteObservation.quote_id
    source_channel: SourceChannel = SourceChannel.MANUAL
    firm_vs_estimate: str = "firm"  # firm | estimate (never promoted)
    premium: PremiumNormalized = Field(default_factory=PremiumNormalized)
    coverage_ledger: CoverageLedger = Field(default_factory=CoverageLedger)
    normalization_status: NormalizationStatus = NormalizationStatus.PENDING
    normalization_rule_version: str = "1"
    normalized_at: dt.datetime
    # Safe references to the Issue #10 evidence records that fed this quote.
    source_evidence_record_ids: list[str] = Field(default_factory=list)
    content_hash: str
    idempotency_key: str
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# API-safe views (never expose internals/PII)
# ---------------------------------------------------------------------------


class CoverageLedgerItemView(SensitiveBaseModel):
    model_config = ConfigDict(extra="forbid")

    item_key: str
    state: str
    value: Optional[dict[str, Any]] = None
    provenance: str
    raw_labels: list[str] = Field(default_factory=list)
    source_evidence_ids: list[str] = Field(default_factory=list)


class CoverageLedgerView(SensitiveBaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CoverageLedgerItemView] = Field(default_factory=list)
    unmapped_coverage: list[dict[str, Any]] = Field(default_factory=list)


class PremiumView(SensitiveBaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_presented_amount: Optional[str] = None  # Decimal as string
    provider_presented_frequency: Optional[str] = None
    normalized_annual_amount: Optional[str] = None
    currency: Optional[str] = None
    annualized: bool = False
    derivation: str
    derivation_rule: Optional[str] = None


class NormalizedQuoteView(SensitiveBaseModel):
    """Safe API projection of a normalized quote (no PII, no raw references)."""

    model_config = ConfigDict(extra="forbid")

    normalized_quote_id: str
    intake_session_id: Optional[str] = None
    plan_id: Optional[str] = None
    planned_route_id: Optional[str] = None
    registry_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    aggregator_registry_id: Optional[str] = None
    presented_carrier: Optional[str] = None
    attempt_id: Optional[str] = None
    parent_attempt_id: Optional[str] = None
    source_quote_observation_id: str
    source_channel: str
    firm_vs_estimate: str
    premium: PremiumView
    coverage_ledger: CoverageLedgerView
    normalization_status: str
    normalization_rule_version: str
    normalized_at: dt.datetime
    source_evidence_record_ids: list[str] = Field(default_factory=list)
    content_hash: str


class NormalizedExportView(SensitiveBaseModel):
    """Safe, PII-free export of normalized quotes (separate from raw evidence)."""

    model_config = ConfigDict(extra="forbid")

    intake_session_id: str
    exported_at: dt.datetime
    normalized_quote_count: int
    distinct_attempts: list[str] = Field(default_factory=list)
    distinct_routes: list[str] = Field(default_factory=list)
    normalization_rule_version: str
    quotes: list[NormalizedQuoteView] = Field(default_factory=list)
