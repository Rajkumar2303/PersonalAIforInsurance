"""Browser route configuration models (Issue #7).

``BrowserRouteConfig`` is the DATA-DRIVEN, typed configuration for one browser
route. Common form changes - question wording, selector changes, new known
canonical questions, disabled fields, new routes using generic controls - are
handled by editing this configuration (or the field catalog + a binding), NEVER
by changing the generic executor. Insurer-specific behavior lives here or in a
site adapter, not in the core executor.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field

from ..insurance.base import SensitiveBaseModel
from ..intake.checkpoints import HumanCheckpointKind
from ..intake.field_catalog import FieldSensitivity
from .session import BrowserActionSafety


class MatchStrategy(StrEnum):
    """Deterministic field-matching strategies (no brittle CSS-only)."""

    LABEL_TEXT = "label_text"
    LABEL_CONTAINS = "label_contains"
    NORMALIZED_LABEL = "normalized_label"
    ARIA_LABEL = "aria_label"
    NAME = "name"
    ID = "id"
    PLACEHOLDER = "placeholder"
    ROLE = "role"
    CSS_SELECTOR = "css_selector"
    TEXT_REGEX = "text_regex"


class FillStrategy(StrEnum):
    """Reusable, deterministic fill strategies."""

    TEXT = "text"
    INTEGER = "integer"
    SELECT = "select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    DATE = "date"
    YES_NO = "yes_no"


class TransformKind(StrEnum):
    """Controlled value transformations (registry, never arbitrary eval)."""

    NONE = "none"
    ENUM_TO_LABEL = "enum_to_label"
    ISO_DATE_TO_DEST = "iso_date_to_dest"
    BOOL_TO_YES_NO = "bool_to_yes_no"
    INTEGER_TO_STRING = "integer_to_string"
    COLLECTION_LENGTH = "collection_length"  # derived len(canonical collection)


class MatchPattern(SensitiveBaseModel):
    """One matching pattern against a visible control."""

    model_config = ConfigDict(extra="forbid")

    strategy: MatchStrategy
    value: str


class BrowserFieldBinding(SensitiveBaseModel):
    """Maps an external website field to a canonical profile path."""

    model_config = ConfigDict(extra="forbid")

    external_field_id: str
    match_patterns: list[MatchPattern] = Field(default_factory=list)
    canonical_path: str
    control_type: str = "input"  # input | select | textarea | radio | checkbox
    fill_strategy: FillStrategy = FillStrategy.TEXT
    transform: TransformKind = TransformKind.NONE
    required: bool = True
    sensitivity: FieldSensitivity = FieldSensitivity.PERSONAL
    enabled: bool = True
    # Controlled transformation data (deterministic, config-driven).
    option_map: dict[str, str] = Field(default_factory=dict)  # canonical -> label
    date_format: Optional[str] = None  # destination date format for DATE fills


class PageSignatureSpec(SensitiveBaseModel):
    """Deterministic page/step identification (branching-safe)."""

    model_config = ConfigDict(extra="forbid")

    signature_id: str
    url_pattern: Optional[str] = None  # regex against full URL
    heading_patterns: list[str] = Field(default_factory=list)  # substring/regex
    field_ids: list[str] = Field(default_factory=list)  # presence of any field


class ActionBinding(SensitiveBaseModel):
    """Binds a clickable action to a safety classification."""

    model_config = ConfigDict(extra="forbid")

    action_type: str  # e.g. "continue", "submit_quote", ...
    safety: BrowserActionSafety
    label_patterns: list[str] = Field(default_factory=list)  # normalized contains
    enabled: bool = True


class CheckpointBinding(SensitiveBaseModel):
    """Binds page/button signals to a human checkpoint kind."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_type: HumanCheckpointKind
    label_patterns: list[str] = Field(default_factory=list)
    url_patterns: list[str] = Field(default_factory=list)
    enabled: bool = True


class QuoteDetectionConfig(SensitiveBaseModel):
    """Configurable quote/result detection signals."""

    model_config = ConfigDict(extra="forbid")

    heading_patterns: list[str] = Field(default_factory=list)
    premium_label_patterns: list[str] = Field(default_factory=list)
    price_pattern: Optional[str] = None  # regex with optional named group amount
    currency: Optional[str] = None
    reference_patterns: list[str] = Field(default_factory=list)
    coverage_label_patterns: list[str] = Field(default_factory=list)
    discount_label_patterns: list[str] = Field(default_factory=list)
    validity_label_patterns: list[str] = Field(default_factory=list)
    monthly_label_patterns: list[str] = Field(default_factory=list)
    annual_label_patterns: list[str] = Field(default_factory=list)
    firm_quote_patterns: list[str] = Field(default_factory=list)


class CallbackDetectionConfig(SensitiveBaseModel):
    """Signals that the web journey requires phone/callback completion."""

    model_config = ConfigDict(extra="forbid")

    patterns: list[str] = Field(default_factory=list)


class AccessControlDetectionConfig(SensitiveBaseModel):
    """Signals for CAPTCHA / bot / access-control barriers (we STOP)."""

    model_config = ConfigDict(extra="forbid")

    patterns: list[str] = Field(default_factory=list)
    iframe_src_patterns: list[str] = Field(default_factory=list)  # recaptcha/hcaptcha
    selectors: list[str] = Field(default_factory=list)  # explicit barrier selectors


class ValidationDetectionConfig(SensitiveBaseModel):
    """Signals for a website rejecting a filled value (we PAUSE, never loop)."""

    model_config = ConfigDict(extra="forbid")

    patterns: list[str] = Field(default_factory=list)
    selectors: list[str] = Field(default_factory=list)  # explicit error selectors


class BrowserRouteConfig(SensitiveBaseModel):
    """Typed, validated configuration for one browser route."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    config_version: int = 1
    start_url: Optional[str] = None  # overrides/validates registry quote_url
    allowed_hosts: list[str] = Field(default_factory=list)
    page_signatures: list[PageSignatureSpec] = Field(default_factory=list)
    field_bindings: list[BrowserFieldBinding] = Field(default_factory=list)
    action_bindings: list[ActionBinding] = Field(default_factory=list)
    checkpoint_bindings: list[CheckpointBinding] = Field(default_factory=list)
    quote_detection: QuoteDetectionConfig = Field(default_factory=QuoteDetectionConfig)
    callback_detection: CallbackDetectionConfig = Field(default_factory=CallbackDetectionConfig)
    access_control_detection: AccessControlDetectionConfig = Field(default_factory=AccessControlDetectionConfig)
    validation_detection: ValidationDetectionConfig = Field(default_factory=ValidationDetectionConfig)
    automation_notes: Optional[str] = None
    last_verified_at: Optional[dt.datetime] = None
