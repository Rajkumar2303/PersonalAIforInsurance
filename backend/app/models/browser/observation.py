"""Browser observation models (Issue #7).

Issue #7 OBSERVES browser outcomes; it does NOT classify terminal quote
statuses (that is Issue #8). ``BrowserObservationType`` is the smallest
sensible set of observation kinds for the browser layer.

All observations are SAFE: they carry external field ids, canonical paths,
counts, page signatures, and sanitized URLs - never applicant values and never
raw DOM/page HTML.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field

from ..insurance.base import SensitiveBaseModel


class BrowserObservationType(StrEnum):
    """Kinds of browser observation. NOT Issue #8 terminal statuses."""

    PAGE_LOADED = "page_loaded"
    FIELDS_FILLED = "fields_filled"
    NEEDS_FIELD = "needs_field"
    NEEDS_CONSENT = "needs_consent"
    HUMAN_CHECKPOINT = "human_checkpoint"
    ACCESS_CONTROL_DETECTED = "access_control_detected"
    UNKNOWN_EXTERNAL_FIELD = "unknown_external_field"
    QUOTE_DETECTED = "quote_detected"
    CALLBACK_DETECTED = "callback_detected"
    MANUAL_CONTACT_DETECTED = "manual_contact_detected"
    TECHNICAL_ERROR = "technical_error"
    ROUTE_CHANGED = "route_changed"
    COMPLETE_WITHOUT_QUOTE = "complete_without_quote"
    UNSUPPORTED_PAGE = "unsupported_page"
    VALUE_NOT_SUPPORTED = "value_not_supported"
    VALIDATION_ERROR = "validation_error"
    AMBIGUOUS_FIELD = "ambiguous_field"
    AMBIGUOUS_ACTION = "ambiguous_action"


class BrowserFieldObservation(SensitiveBaseModel):
    """Safe metadata for one visible form control. NO input value."""

    model_config = ConfigDict(extra="forbid")

    external_field_id: str
    control_type: str  # input | select | textarea | radio | checkbox | button
    label: Optional[str] = None
    name: Optional[str] = None
    input_type: Optional[str] = None  # text | email | date | number | ...
    placeholder: Optional[str] = None
    required: bool = False
    options_labels: list[str] = Field(default_factory=list)


class BrowserPageObservation(SensitiveBaseModel):
    """Safe snapshot of one inspected page."""

    model_config = ConfigDict(extra="forbid")

    page_index: int = 0
    url: Optional[str] = None  # sanitized (no query string)
    page_signature: Optional[str] = None
    fields: list[BrowserFieldObservation] = Field(default_factory=list)
    controls_count: int = 0
    heading: Optional[str] = None  # only when it matches a known signature


class BrowserCheckpointObservation(SensitiveBaseModel):
    """A human-checkpoint signal (reuses Issue #5 checkpoint semantics)."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_type: str  # HumanCheckpointKind value
    label: str
    requires_human: bool = True
    must_not_automate: bool = False
    action_label: Optional[str] = None


class RawQuoteObservation(SensitiveBaseModel):
    """Raw, unnormalized quote/result observation.

    Deliberately NOT normalized (Issue #11 owns normalization). A quote is only
    claimed as firm when page evidence supports it. The quote/reference
    identifier is user-specific and stays behind a private boundary - only
    ``reference_present`` and an opaque private handle are exposed in safe
    metadata.
    """

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    observed_at: dt.datetime
    source_url: Optional[str] = None
    annual_amount_raw: Optional[str] = None
    annual_amount_parsed: Optional[float] = None
    monthly_amount_raw: Optional[str] = None
    monthly_amount_parsed: Optional[float] = None
    currency: Optional[str] = None
    coverage_observations: list[str] = Field(default_factory=list)
    discount_observations: list[str] = Field(default_factory=list)
    validity_text: Optional[str] = None
    reference_present: bool = False
    private_reference_handle: Optional[str] = None
    is_firm_quote: bool = False  # only when page evidence supports it


class BrowserQuoteObservation(SensitiveBaseModel):
    """Safe workflow wrapper around a raw quote observation."""

    model_config = ConfigDict(extra="forbid")

    quote_present: bool
    reference_present: bool
    raw: RawQuoteObservation


class BrowserObservation(SensitiveBaseModel):
    """One observation produced by a browser step (safe)."""

    model_config = ConfigDict(extra="forbid")

    observation_type: BrowserObservationType
    page_index: int = 0
    page_signature: Optional[str] = None
    url: Optional[str] = None  # sanitized
    message: Optional[str] = None  # safe; never values
    fields: list[BrowserFieldObservation] = Field(default_factory=list)
    filled_field_count: int = 0
    missing_field_paths: list[str] = Field(default_factory=list)  # canonical paths
    needs_consent_paths: list[str] = Field(default_factory=list)  # canonical paths
    pending_field_paths: list[str] = Field(default_factory=list)  # canonical paths
    unknown_external_fields: list[str] = Field(default_factory=list)
    unknown_field_observations: list[BrowserFieldObservation] = Field(default_factory=list)
    ambiguous_field_ids: list[str] = Field(default_factory=list)
    ambiguous_action_labels: list[str] = Field(default_factory=list)
    unsupported_value_paths: list[str] = Field(default_factory=list)  # canonical paths
    error_paths: list[str] = Field(default_factory=list)  # canonical paths (safe)
    checkpoint: Optional[BrowserCheckpointObservation] = None
    quote: Optional[BrowserQuoteObservation] = None
