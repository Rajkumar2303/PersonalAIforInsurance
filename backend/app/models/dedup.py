"""Rate-source deduplication models (Issue #4).

Purpose: represent distinct underlying rate sources and the evidence-backed,
deterministic relationships between market routes and those sources.

Key distinctions kept separate on purpose:
- ``DeduplicationStatus`` (unique / duplicate_confirmed / duplicate_possible /
  unresolved) is about RATE-SOURCE identity - NOT registry lifecycle
  (``RegistryStatus``) and NOT quote-outcome terminal statuses.

Data-flow contract:
- ``MarketRegistryEntry.distinct_rate_source_id`` (Issue #3) is the AUTHORITATIVE
  route -> source mapping (single source of truth).
- ``DistinctRateSource.related_registry_ids`` is supplementary evidence; the
  service validates on load that it never contradicts the registry mapping.

These models contain NO applicant PII - only public market data and evidence
references.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .insurance.enums import InsuranceType


class DeduplicationStatus(StrEnum):
    """Confidence state of a rate-source relationship (pair or route)."""

    UNIQUE = "unique"  # confirmed distinct program/rate source
    DUPLICATE_CONFIRMED = "duplicate_confirmed"  # same underlying rate source
    DUPLICATE_POSSIBLE = "duplicate_possible"  # candidate only; needs evidence
    UNRESOLVED = "unresolved"  # no evidence either way


class Confidence(StrEnum):
    HIGH = "high"  # explicit verified evidence
    MEDIUM = "medium"  # strong relationship evidence, not complete
    LOW = "low"  # candidate only / insufficient


class ReasonCode(StrEnum):
    """Explainable reason for a deduplication decision."""

    SAME_VERIFIED_RATE_SOURCE = "same_verified_rate_source"
    SAME_VERIFIED_PROGRAM = "same_verified_program"
    SAME_UNDERWRITER_POSSIBLE_DUPLICATE = "same_underwriter_possible_duplicate"
    SAME_GROUP_ONLY_INSUFFICIENT = "same_group_only_insufficient"
    EXPLICITLY_DISTINCT_PROGRAM = "explicitly_distinct_program"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DistinctRateSource(BaseModel):
    """One distinct underlying rate source (may have many routes/brands).

    Minimal by design - no speculative fields. ``related_registry_ids`` is
    evidence metadata only; the authoritative mapping lives on
    ``MarketRegistryEntry.distinct_rate_source_id``.
    """

    model_config = ConfigDict(extra="forbid")

    distinct_rate_source_id: str
    product_type: InsuranceType = InsuranceType.AUTO
    insurer_group: Optional[str] = None
    legal_underwriters: list[str] = Field(default_factory=list)
    program_name: Optional[str] = None
    related_registry_ids: list[str] = Field(default_factory=list)
    deduplication_status: DeduplicationStatus = DeduplicationStatus.UNIQUE
    confidence: Confidence = Confidence.LOW
    evidence_references: list[str] = Field(default_factory=list)
    source_citations: list[str] = Field(default_factory=list)
    last_verified_at: Optional[dt.datetime] = None
    notes: Optional[str] = None

    @field_validator("distinct_rate_source_id")
    @classmethod
    def _normalize_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("distinct_rate_source_id must not be empty")
        return normalized

    @field_validator("related_registry_ids")
    @classmethod
    def _normalize_related(cls, value: list[str]) -> list[str]:
        return sorted(set(item.strip() for item in value if item.strip()))


class DeduplicationDecision(BaseModel):
    """Explainable answer to: why are these two routes duplicates (or not)?"""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    candidate_registry_id: str
    decision: DeduplicationStatus
    distinct_rate_source_id: Optional[str] = None
    reason_code: ReasonCode
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence
    evaluated_at: dt.datetime


class DuplicateCandidate(BaseModel):
    """A POSSIBLE duplicate (candidate detection - NOT confirmation)."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    reason_code: ReasonCode
    distinct_rate_source_id: Optional[str] = None
    confidence: Confidence


class DuplicateGroup(BaseModel):
    """Registry routes confirmed to share one distinct rate source."""

    model_config = ConfigDict(extra="forbid")

    distinct_rate_source_id: str
    registry_ids: list[str]


class DeduplicatedMarket(BaseModel):
    """One row in the deduplicated market view.

    Confirmed duplicates collapse to a single representative; possible and
    unresolved routes each stay visible as their own row.
    """

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    distinct_rate_source_id: Optional[str] = None
    deduplication_status: DeduplicationStatus
    group_members: list[str] = Field(default_factory=list)
