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

from pydantic import model_validator

from .auto.profile import AutoInsuranceProfile
from .base import SCHEMA_VERSION, SensitiveBaseModel
from .common import ApplicantInformation, ConsentState
from .enums import InsuranceType


def _resolve_path(obj: Any, path: tuple[str | int, ...]) -> Any:
    """Resolve a dotted path (int segments index lists) against a model."""
    current = obj
    for segment in path:
        if isinstance(segment, int):
            current = current[segment]
        else:
            current = getattr(current, segment)
    return current


def _path_missing(value: Any) -> bool:
    """A value is 'missing' when unset (None/empty string/empty collection)."""
    if value is None or value == "":
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


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

    # --- lightweight schema-layer helpers (full intake engine = Issue #5) ---

    def required_for_live_quote(self) -> tuple[tuple[str | int, ...], ...]:
        """Curated set of field paths considered required for a live quote.

        This is intentionally a small, schema-level definition - the intake
        engine (Issue #5) will drive per-route requirements dynamically.
        """
        return (
            ("consent", "consent_timestamp"),
            ("consent", "quote_mode"),
            ("applicant", "identity", "legal_name"),
            ("applicant", "identity", "date_of_birth"),
            ("applicant", "address", "postal_code"),
            ("product_data", "drivers", 0, "licence", "licence_number"),
            ("product_data", "vehicles", 0, "identity", "vin"),
        )

    def get_missing_fields(self) -> set[str]:
        """Return the ``required_for_live_quote`` paths that are currently unset."""
        missing: set[str] = set()
        for path in self.required_for_live_quote():
            try:
                value = _resolve_path(self, path)
            except (AttributeError, IndexError, TypeError):
                missing.add(".".join(str(segment) for segment in path))
                continue
            if _path_missing(value):
                missing.add(".".join(str(segment) for segment in path))
        return missing

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
