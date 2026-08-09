"""Ontario market registry models (Issue #3).

The registry is a DATA-DRIVEN discovery seed of PUBLIC market information only.
It is a separate bounded concept from ``InsuranceProfile`` and contains NO
applicant PII (no licence numbers, DOB, addresses, VINs, claims, or participant
contact data).

Terminology follows the hackathon brief (Appendix A/B): legal underwriter,
insurer group, consumer brand, distributor/aggregator/MGA/mutual/residual, and
a nullable ``distinct_rate_source_id`` for Issue #4 deduplication.

``RegistryStatus`` is the registry lifecycle status (discovered/verified/...)
and is intentionally SEPARATE from quote-attempt terminal statuses.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .insurance.enums import InsuranceType


class DistributionType(StrEnum):
    """How the brand/program reaches the applicant (brief Appendix B)."""

    DIRECT = "direct"
    AGENT = "agent"
    BROKER = "broker"
    AGGREGATOR = "aggregator"
    AFFINITY = "affinity"
    MGA_PROGRAM = "mga_program"
    MUTUAL = "mutual"
    RESIDUAL = "residual"


class ProductScope(StrEnum):
    """What kind of auto risk the route targets (brief Appendix B)."""

    STANDARD_PPA = "standard_PPA"
    NONSTANDARD_PPA = "nonstandard_PPA"
    HIGH_NET_WORTH = "high_net_worth"
    COLLECTOR = "collector"
    COMMERCIAL_SPECIALTY = "commercial_specialty"
    UNKNOWN = "unknown"


class RegistryStatus(StrEnum):
    """Registry lifecycle / verification status.

    Distinct from quote-attempt terminal statuses: this is about whether the
    ROUTE has been verified, not how a quote attempt ended.
    """

    DISCOVERED = "discovered"  # seeded from the brief; not yet verified
    VERIFIED = "verified"  # verified during the hackathon (last_verified_at set)
    STALE = "stale"  # previously verified but now out of date
    INACTIVE = "inactive"  # no longer applicable
    UNKNOWN = "unknown"


class MarketRequirement(StrEnum):
    """Extensible requirement a route may impose on the profile/journey."""

    LICENCE = "licence"
    VIN = "vin"
    MEMBERSHIP = "membership"
    CALLBACK = "callback"
    HUMAN = "human"
    OTHER = "other"


class MarketRegistryEntry(BaseModel):
    """One machine-readable market route (discovery seed).

    ``distinct_rate_source_id`` is nullable: it must NOT be guessed. It is
    populated only after Issue #4 verification maps the route to a distinct
    underlying rate source.
    """

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    product_type: InsuranceType = InsuranceType.AUTO
    legal_underwriter: Optional[str] = None
    insurer_group: Optional[str] = None
    brand_or_program: str
    distribution_type: DistributionType
    product_scope: ProductScope = ProductScope.UNKNOWN
    distinct_rate_source_id: Optional[str] = None
    quote_url: Optional[str] = None
    public_phone_route: Optional[str] = None
    callback_route: Optional[str] = None
    known_panel_source: Optional[str] = None
    licensed_intermediary: Optional[str] = None
    requirements: list[MarketRequirement] = Field(default_factory=list)
    automation_notes: Optional[str] = None
    status: RegistryStatus = RegistryStatus.DISCOVERED
    source_url: Optional[str] = None
    source_citation: Optional[str] = None
    last_verified_at: Optional[dt.datetime] = None
    evidence_artifact: Optional[str] = None
    active: bool = True

    @field_validator("registry_id")
    @classmethod
    def _normalize_registry_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("registry_id must not be empty")
        return normalized

    @field_validator("requirements")
    @classmethod
    def _normalize_requirements(cls, value: list[MarketRequirement]) -> list[MarketRequirement]:
        """Deduplicate and sort for deterministic JSON output."""
        return sorted(set(value), key=lambda item: item.value)

    @model_validator(mode="after")
    def _check_verified_timestamp(self) -> "MarketRegistryEntry":
        if self.status is RegistryStatus.VERIFIED and self.last_verified_at is None:
            raise ValueError("verified records require last_verified_at")
        return self
