"""Consent receipts (Issue #5).

Consent is explicit and structured - a single ``consent = true`` is never
treated as authorization for every destination forever. Receipts distinguish:

- ``collection``: permission to collect/store applicant information,
- ``route_disclosure``: permission to disclose specific fields to a route,
- ``household_driver``: applicant attestation that another household driver
  consented to collection/use/disclosure.

Receipts store PATHS and metadata - NEVER field values. They are kept separate
from quote data.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Optional

from pydantic import ConfigDict, Field

from ..insurance.base import SensitiveBaseModel


class ConsentScope(StrEnum):
    COLLECTION = "collection"
    ROUTE_DISCLOSURE = "route_disclosure"
    HOUSEHOLD_DRIVER = "household_driver"


class ConsentReceipt(SensitiveBaseModel):
    """A structured, revocable consent record. No field values."""

    model_config = ConfigDict(extra="forbid")

    consent_id: str
    session_id: str
    profile_id: Optional[str] = None
    scope: ConsentScope
    route_registry_id: Optional[str] = None
    canonical_field_paths: list[str] = Field(default_factory=list)  # paths, not values
    granted: bool
    timestamp: dt.datetime
    revoked_at: Optional[dt.datetime] = None
    subject_reference: Optional[str] = None  # e.g. "principal" or a driver label
    purpose: Optional[str] = None  # safe purpose description


class RouteDisclosureConsent(SensitiveBaseModel):
    """Thin route-scoped view over a route_disclosure receipt."""

    model_config = ConfigDict(extra="forbid")

    consent_id: str
    route_registry_id: str
    registry_name: Optional[str] = None
    canonical_field_paths: list[str] = Field(default_factory=list)
    granted: bool
    timestamp: dt.datetime
    revoked_at: Optional[dt.datetime] = None
