"""Comparison run domain (Issue #13, MVP).

A ``ComparisonRun`` coordinates independent provider routes, collects their
outcomes, auto-normalizes quotes, and produces a comparison. It carries SAFE
result metadata only - never applicant PII, never raw quote references.

Lifecycle: ``prepared -> running -> completed | completed_with_partial_results |
failed``. One route failing (CAPTCHA, exception, missing info) never stops the
other routes; that is the orchestrator's contract, not this model's.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field

from .comparison import ComparisonPlanResult
from .insurance.base import SensitiveBaseModel


class ComparisonRunStatus(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_PARTIAL_RESULTS = "completed_with_partial_results"
    FAILED = "failed"


class RouteRunStatus(StrEnum):
    """Frontend-safe per-route state (reuses the terminal vocabulary)."""

    QUEUED = "queued"
    RUNNING = "running"
    QUOTE_PENDING_NORMALIZATION = "quote_pending_normalization"
    COMPARABLE = "comparable"
    NON_COMPARABLE = "non_comparable"
    ESTIMATE_ONLY = "estimate_only"
    DUPLICATE_RATE_SOURCE = "duplicate_rate_source"
    CAPTCHA_BLOCKED = "captcha_blocked"
    UNAVAILABLE = "unavailable"
    CALLBACK_REQUIRED = "callback_required"
    MANUAL_HANDOFF = "manual_handoff"
    NEEDS_ADDITIONAL_INFORMATION = "needs_additional_information"
    INELIGIBLE = "ineligible"
    NOT_CURRENTLY_WRITING = "not_currently_writing"
    AFFINITY_RESTRICTED = "affinity_restricted"
    SPECIALTY_ONLY = "specialty_only"
    NOT_READY = "not_ready"
    CONSENT_REQUIRED = "consent_required"
    UNRESOLVED = "unresolved"
    FAILED = "failed"
    # Route is deliberately NOT executed by the one-shot comparison run; it is
    # operator-managed (e.g. Sonnet live: started only via the Run Sonnet Live
    # control, never through Compare Quotes). Visible in the ledger as ready.
    OPERATOR_MANAGED = "operator_managed"


class RouteRunSummary(SensitiveBaseModel):
    """One route's outcome (frontend-safe; no applicant values)."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    display_name: str
    channel: str = "browser"  # browser | voice | manual
    status: RouteRunStatus = RouteRunStatus.QUEUED
    route_outcome_semantics: Optional[str] = None  # quoted_comparable | ...
    terminal_status: Optional[str] = None
    reason_codes: list[str] = Field(default_factory=list)
    annual_premium: Optional[Decimal] = None
    firm_vs_estimate: str = "firm"
    coverage_summary: dict[str, str] = Field(default_factory=dict)
    missing_coverage_keys: list[str] = Field(default_factory=list)
    distinct_rate_source_id: Optional[str] = None
    aggregator_registry_id: Optional[str] = None
    is_alternative: bool = False
    is_representative: bool = False
    evidence_status: str = "unavailable"  # recorded | unavailable
    quote_observation_id: Optional[str] = None
    source_quote_observation_id: Optional[str] = None
    normalized_quote_id: Optional[str] = None
    pending_field_paths: list[str] = Field(default_factory=list)
    message: Optional[str] = None  # safe, non-sensitive


class ComparisonRun(SensitiveBaseModel):
    """Pollable state of one comparison run (safe)."""

    model_config = ConfigDict(extra="forbid")

    comparison_run_id: str
    intake_session_id: str
    plan_id: Optional[str] = None
    execution_mode: str = "mock"
    status: ComparisonRunStatus = ComparisonRunStatus.PREPARED
    created_at: dt.datetime
    started_at: Optional[dt.datetime] = None
    completed_at: Optional[dt.datetime] = None
    total_routes: int = 0
    completed_routes: int = 0
    running_routes: int = 0
    route_summaries: list[RouteRunSummary] = Field(default_factory=list)
    comparison: Optional[ComparisonPlanResult] = None
    error: Optional[str] = None  # safe
