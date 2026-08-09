"""Top-level canonical insurance profile.

``InsuranceProfile`` holds the shared (product-agnostic) data plus a
product-specific profile:

    InsuranceProfile
    ├── insurance_type
    ├── schema_version
    ├── consent            (shared)
    ├── applicant          (shared identity/contact/address)
    └── product_data       (AutoInsuranceProfile | None for unsupported types)

Only AUTO is implemented. HOME/TENANT/LIFE/TRAVEL/OTHER are recognized by
``InsuranceType`` but carry ``product_data=None`` and report
``is_supported == False``.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import ValidationError, model_validator

from .auto.profile import AutoInsuranceProfile
from .base import SCHEMA_VERSION, SensitiveBaseModel
from .common import ApplicantInformation, ConsentState
from .enums import InsuranceType
from .paths import FieldPathError, is_missing, parse_field_path


class ProfileUpdateError(ValueError):
    """Raised when a validated dynamic update fails.

    The message carries only the canonical field path - never the rejected
    value - so sensitive applicant data is never exposed in errors or logs.
    """

    def __init__(self, path: str) -> None:
        super().__init__(f"invalid value for canonical field path {path!r}")
        self.path = path


def _set_in_dict(data: dict[str, Any], segments: tuple[str | int, ...], value: Any, path: str) -> None:
    """Set ``value`` at ``segments`` inside a dumped model dict.

    Rejects unknown fields and out-of-range indexes (``FieldPathError``).
    """
    current: Any = data
    for index, segment in enumerate(segments):
        is_last = index == len(segments) - 1
        if isinstance(segment, int):
            if not isinstance(current, list):
                raise FieldPathError(f"cannot index a non-list at {path!r}")
            if segment < 0 or segment >= len(current):
                raise FieldPathError(f"list index {segment} out of range in path {path!r}")
            if is_last:
                current[segment] = value
            else:
                current = current[segment]
        else:
            if not isinstance(current, dict):
                raise FieldPathError(f"cannot traverse a non-object at {path!r}")
            if segment not in current:
                raise FieldPathError(f"unknown field {segment!r} in path {path!r}")
            if is_last:
                current[segment] = value
            else:
                current = current[segment]


class InsuranceProfile(SensitiveBaseModel):
    """Canonical, product-aware insurance intake profile (Issue #2)."""

    schema_version: str = SCHEMA_VERSION
    insurance_type: InsuranceType
    consent: ConsentState
    applicant: ApplicantInformation
    product_data: Optional[AutoInsuranceProfile] = None

    @model_validator(mode="after")
    def _validate_product_support(self) -> "InsuranceProfile":
        if self.insurance_type is InsuranceType.AUTO and self.product_data is None:
            raise ValueError("AUTO insurance_type requires product_data (AutoInsuranceProfile)")
        if self.insurance_type is not InsuranceType.AUTO and self.product_data is not None:
            raise ValueError(
                f"unsupported insurance_type '{self.insurance_type.value}' cannot carry product_data"
            )
        return self

    @property
    def is_supported(self) -> bool:
        """True only for products with a fully implemented schema (AUTO)."""
        return self.insurance_type is InsuranceType.AUTO

    # --- draft vs live-quote readiness -----------------------------------

    @property
    def is_draft(self) -> bool:
        """True until the profile has everything required for a live quote."""
        return not self.is_live_quote_ready

    @property
    def is_live_quote_ready(self) -> bool:
        """True for a supported product with no missing live-quote fields."""
        return self.is_supported and not self.get_missing_fields()

    # --- lightweight schema-layer helpers (full intake engine = Issue #5) ---

    def required_for_live_quote(self) -> tuple[str, ...]:
        """Canonical paths considered required for a live quote.

        Uses the canonical field-path convention (see ``paths.py``). This is a
        small schema-level definition; the intake engine (Issue #5) will drive
        per-route requirements dynamically.
        """
        return (
            "consent.consent_timestamp",
            "consent.quote_mode",
            "applicant.identity.legal_name",
            "applicant.identity.date_of_birth",
            "applicant.address.postal_code",
            "applicant.address.street",
            "applicant.address.city",
            "product_data.drivers[0].licence.licence_number",
            "product_data.vehicles[0].identity.vin",
        )

    def get_missing_fields(self) -> set[str]:
        """Return the ``required_for_live_quote`` canonical paths that are unset."""
        return {path for path in self.required_for_live_quote() if is_missing(self, path)}

    # --- validated dynamic update (Issue #3) -------------------------------

    def updated(self, path: str, value: Any) -> "InsuranceProfile":
        """Return a NEW validated profile with ``value`` set at ``path``.

        - Rejects unknown fields / bad indexes (``FieldPathError``).
        - Revalidates the whole profile through Pydantic, so invalid values and
          product invariants fail loudly (unlike ``model_copy(update=...)``,
          which does NOT revalidate in Pydantic v2).
        - Preserves unrelated profile data.
        - Raises ``ProfileUpdateError`` (path only, never the value) on failure.
        """
        segments = parse_field_path(path)
        data = self.model_dump(mode="python")
        _set_in_dict(data, segments, value, path)
        try:
            return type(self).model_validate(data)
        except ValidationError as exc:
            raise ProfileUpdateError(path) from exc

    def set_field(self, path: str, value: Any) -> "InsuranceProfile":
        """Alias for ``updated`` - a validated, immutable single-field update."""
        return self.updated(path, value)

    def trace_metadata(self) -> dict[str, Any]:
        """Safe, non-sensitive metadata for LangSmith / logs in later issues.

        Never includes field values; only identifiers/counts.
        """
        return {
            "insurance_type": self.insurance_type.value,
            "schema_version": self.schema_version,
            "is_supported": self.is_supported,
            "missing_field_count": len(self.get_missing_fields()),
        }
