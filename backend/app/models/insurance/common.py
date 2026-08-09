"""Shared (product-agnostic) applicant, contact, address, and consent models.

These live at the ``InsuranceProfile`` level so future product schemas (home,
tenant, life, travel) reuse them instead of duplicating shared fields. Only
the product-specific parts go inside each product profile (e.g. AUTO).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from pydantic import Field, field_validator

from .base import SensitiveBaseModel
from .enums import ChannelType, Gender, MaritalStatus, PreferredLanguage, Province, QuoteMode

# Canadian postal code: A1A 1A1 (space optional on input; normalized to a space).
_POSTAL_CODE_RE = re.compile(r"^[A-Za-z]\d[A-Za-z]\d[A-Za-z]\d$")


class ConsentState(SensitiveBaseModel):
    """Consent + journey mode for the quote attempt.

    Universal-required at the profile level (consent-aware intake). Kept
    deliberately simple for Issue #2; expands in later issues if needed.
    """

    consent_timestamp: dt.datetime
    quote_mode: QuoteMode
    permitted_channels: list[ChannelType] = Field(default_factory=list)
    approved_insurers_or_brokers: Optional[list[str]] = None
    callback_permission: bool = False
    recording_permission: bool = False
    transcription_permission: bool = False


class ApplicantIdentity(SensitiveBaseModel):
    """Personal identity of the applicant.

    SENSITIVE: legal_name, date_of_birth (redacted in safe output).

    ``date_of_birth`` is optional so a DRAFT profile can be created before the
    live-quote route requires it; validation is unchanged when a value is set.
    """

    legal_name: str
    alias: Optional[str] = None
    preferred_language: PreferredLanguage = PreferredLanguage.ENGLISH
    date_of_birth: Optional[dt.date] = None
    gender: Optional[Gender] = None
    marital_status: Optional[MaritalStatus] = None


class ContactInformation(SensitiveBaseModel):
    """Contact details. SENSITIVE: all phone/email fields (redacted)."""

    email: Optional[str] = None
    mobile_phone: Optional[str] = None
    home_phone: Optional[str] = None
    work_phone: Optional[str] = None
    preferred_callback_window: Optional[str] = None


class AddressInformation(SensitiveBaseModel):
    """Primary residence address. SENSITIVE: whole block (redacted).

    ``street``/``city`` are optional so a DRAFT profile can start with just a
    postal code; validators (postal format) still run when values are supplied.

    ``years_at_current_address`` (optional, Issue #5) is the canonical home for
    the commonly-discovered question "how many years have you lived at this
    address?". It is added as a localized optional field + a data-driven
    catalog entry - no intake workflow special-casing.
    """

    street: Optional[str] = None
    unit: Optional[str] = None
    city: Optional[str] = None
    province: Province
    postal_code: str
    years_at_current_address: Optional[int] = Field(default=None, ge=0, le=100)
    residence_start_date: Optional[dt.date] = None
    prior_address: Optional["AddressInformation"] = None
    normal_residence_confirmation: bool = False
    garaging_location_confirmation: bool = False

    @field_validator("postal_code")
    @classmethod
    def _validate_postal_code(cls, value: str) -> str:
        code = value.strip().upper().replace(" ", "")
        if not _POSTAL_CODE_RE.fullmatch(code):
            raise ValueError("invalid Canadian postal code (expected A1A 1A1)")
        return f"{code[:3]} {code[3:]}"


class ApplicantInformation(SensitiveBaseModel):
    """Composition of the shared applicant identity/contact/address."""

    identity: ApplicantIdentity
    contact: ContactInformation
    address: AddressInformation
