"""Data-driven intake field catalog models (Issue #5).

The catalog is the single source of truth for HOW TO ASK a question. The
canonical Pydantic schema (Issue #2/#3) remains authoritative for HOW TO
VALIDATE an answer - the catalog never duplicates validation rules.

A ``canonical_path_template`` may contain a list-index placeholder such as
``{vehicle_index}``. It is resolved to a concrete canonical path (index 0 by
default in Issue #5) before asking/updating.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field, field_validator

from ..insurance.base import SensitiveBaseModel
from ..insurance.enums import InsuranceType


class InputType(StrEnum):
    """Deterministic input control hint for the question (how to ask)."""

    TEXT = "text"
    INTEGER = "integer"
    FLOAT = "float"
    DATE = "date"
    YEARS = "years"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    POSTAL_CODE = "postal_code"
    PHONE = "phone"
    EMAIL = "email"
    VIN = "vin"
    LICENCE = "licence"
    CURRENCY = "currency"
    LONG_TEXT = "long_text"


class FieldSensitivity(StrEnum):
    """Privacy classification used for disclosure previews and trace metadata.

    NOTE: this is classification metadata. Actual redaction enforcement stays in
    ``app/core/redaction.py`` + the schema-level sensitive registry.
    """

    NON_SENSITIVE = "non_sensitive"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


class CollectionGroup(StrEnum):
    """Logical grouping for the UI / disclosure previews."""

    IDENTITY = "identity"
    CONTACT = "contact"
    ADDRESS = "address"
    DRIVER = "driver"
    VEHICLE = "vehicle"
    HISTORY = "history"
    COVERAGE = "coverage"
    HOUSEHOLD = "household"
    OTHER = "other"


class IntakePhase(StrEnum):
    """When a field is collected.

    - ``starter``: useful core information to establish the profile and begin
      shopping (collected automatically in priority order).
    - ``route_specific``: collected just-in-time when a route/broker/voice agent
      requests it (never auto-asked up front).
    - ``sensitive_late``: sensitive fields deferred; asked when a route needs
      them or at the appropriate point in the journey.
    """

    STARTER = "starter"
    ROUTE_SPECIFIC = "route_specific"
    SENSITIVE_LATE = "sensitive_late"


class IntakeFieldDefinition(SensitiveBaseModel):
    """One data-driven intake question definition."""

    model_config = ConfigDict(extra="forbid")

    field_id: str
    product_type: InsuranceType = InsuranceType.AUTO
    canonical_path_template: str
    question: str
    short_label: str
    input_type: InputType = InputType.TEXT
    sensitivity: FieldSensitivity = FieldSensitivity.PERSONAL
    collection_group: CollectionGroup = CollectionGroup.OTHER
    intake_phase: IntakePhase = IntakePhase.ROUTE_SPECIFIC
    priority: int = 100
    help_text: Optional[str] = None
    choices: list[str] = Field(default_factory=list)
    enum_source: Optional[str] = None
    enabled: bool = True

    # --- generic list-item assembly (Issue #5) --------------------------
    # Identity-bearing items (drivers, vehicles) cannot exist partially in the
    # canonical schema, so the engine materializes them from the catalog's
    # required fields. ``item_unit`` names the typed container (e.g.
    # "vehicle"), ``item_index_placeholder`` is the template placeholder that
    # indexes into it (e.g. "vehicle_index"), and ``item_unit_required`` marks
    # the fields required to build a valid item. This is entirely catalog
    # driven - there is no per-field logic in the engine.
    item_unit: Optional[str] = None
    item_index_placeholder: Optional[str] = None
    item_unit_required: bool = False

    # --- safety boundaries ----------------------------------------------
    # When True, this field's data cannot be collected until an explicit
    # household-driver consent attestation exists (Issue #5).
    household_attestation_required: bool = False

    # --- seed (Issue #5) ------------------------------------------------
    # Fields required to even materialize a canonical profile (e.g. legal_name
    # and postal_code are required by the schema). Collected before the vault
    # profile is created. Catalog-driven, not a per-field branch.
    seed_required: bool = False

    @field_validator("field_id")
    @classmethod
    def _field_id_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field_id must not be empty")
        return value.strip()
