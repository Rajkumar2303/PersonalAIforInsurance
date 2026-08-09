"""Route planner models (Issue #6).

A ``RoutePlan`` is a deterministic, evidence-first pre-flight view of the
Ontario AUTO market for one intake session. It contains CANONICAL FIELD PATHS
and PUBLIC market data only - never applicant values (rule 8).

Key semantics (Issue #6 critical rules):
- Readiness is PER ROUTE - a global profile need not be live-quote ready for a
  route to be ready (rules 1-2).
- A route can have MULTIPLE blockers simultaneously (rule 3).
- Confirmed duplicates group under one representative (group members listed);
  possible/unresolved duplicates stay visible as their own routes - nothing is
  suppressed (rules 4-5).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional, TypedDict

from pydantic import ConfigDict, Field

from .insurance.base import SensitiveBaseModel
from .insurance.enums import InsuranceType


class RouteBlockerKind(StrEnum):
    """A reason a route is not yet ready (planning-level pre-flight blocker).

    Names align with the quote terminal-status vocabulary where applicable
    (affinity_restricted, callback_required, specialty_only) but remain
    planning scope - no quote attempt happens here.
    """

    MISSING_FIELD = "missing_field"
    CONSENT_REQUIRED = "consent_required"
    RATE_SOURCE_UNRESOLVED = "rate_source_unresolved"
    AFFINITY_RESTRICTED = "affinity_restricted"
    CALLBACK_REQUIRED = "callback_required"
    HUMAN_REQUIRED = "human_required"
    SPECIALTY_ONLY = "specialty_only"
    OTHER = "other"


class RouteChannelKind(StrEnum):
    """Public ways to reach a route (from registry market data).

    - online: web quote url
    - phone / callback / broker: human-assisted channels
    - human: route requires human interaction (MarketRequirement.HUMAN)
    - discovery_only: no direct quote channel (aggregator/discovery only)
    """

    ONLINE = "online"
    PHONE = "phone"
    CALLBACK = "callback"
    BROKER = "broker"
    HUMAN = "human"
    DISCOVERY_ONLY = "discovery_only"


class RouteBlocker(SensitiveBaseModel):
    """One blocker - safe text + (for missing fields) a canonical path only."""

    model_config = ConfigDict(extra="forbid")

    kind: RouteBlockerKind
    reason: str
    canonical_path: Optional[str] = None  # only for missing_field


class RouteChannel(SensitiveBaseModel):
    """A public route channel (quote url / phone / callback / broker)."""

    model_config = ConfigDict(extra="forbid")

    kind: RouteChannelKind
    label: str
    value: Optional[str] = None  # public market data, never applicant PII


class PlannedRoute(SensitiveBaseModel):
    """One planned route (post-dedup). No applicant values."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    brand_or_program: str
    legal_underwriter: Optional[str] = None
    insurer_group: Optional[str] = None
    distribution_type: str
    product_scope: str
    distinct_rate_source_id: Optional[str] = None
    deduplication_status: str
    group_members: list[str] = Field(default_factory=list)
    channels: list[RouteChannel] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)  # canonical paths
    blockers: list[RouteBlocker] = Field(default_factory=list)
    is_ready: bool = False
    is_alternative: bool = False  # True for a non-representative member of a
    # confirmed duplicate group (same distinct rate source as the primary)
    rank: int = 0
    route_status: str = "blocked"  # "ready" | "blocked"


class RoutePlanSummary(SensitiveBaseModel):
    """Deterministic counts (safe)."""

    model_config = ConfigDict(extra="forbid")

    raw_registry_count: int = 0
    planned_route_count: int = 0
    ready_count: int = 0
    blocked_count: int = 0
    confirmed_duplicate_groups: int = 0
    alternative_route_count: int = 0
    unresolved_rate_sources: int = 0
    possible_duplicate_routes: int = 0
    missing_field_paths_count: int = 0


class RoutePlan(SensitiveBaseModel):
    """The full deterministic route plan (paths + public market data only)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    insurance_type: InsuranceType
    routes: list[PlannedRoute] = Field(default_factory=list)
    required_missing_paths: list[str] = Field(default_factory=list)  # canonical paths
    summary: RoutePlanSummary = Field(default_factory=RoutePlanSummary)
    generated_at: dt.datetime


class RoutePlanWorkflowState(TypedDict, total=False):
    """SAFE METADATA ONLY - counts and registry ids; never applicant values."""

    entry: str
    session_id: str
    request_id: Optional[str]
    workflow_stage: str
    workflow_status: str
    message: Optional[str]
    insurance_type: Optional[str]
    planned_route_count: int
    ready_route_count: int
    blocked_route_count: int
    ready_registry_ids: list[str]
    missing_field_path_count: int
