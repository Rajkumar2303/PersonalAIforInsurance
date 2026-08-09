"""Route data-sharing preview models (Issue #5).

A reusable primitive for Issue #6 (route planning) and Issue #7 (browser
agent): before any personal data is submitted to a route, the application can
generate a ``RouteDataDisclosure`` so the applicant can APPROVE or EXCLUDE the
route. This issue never submits anything - it only creates the consent /
disclosure mechanism.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import ConfigDict, Field

from ..insurance.base import SensitiveBaseModel
from .field_catalog import FieldSensitivity


class RouteDataDisclosureItem(SensitiveBaseModel):
    """One field that would be shared - a PATH + label + classification, never
    the value."""

    model_config = ConfigDict(extra="forbid")

    canonical_path: str
    label: str
    sensitivity: FieldSensitivity


class RouteDataDisclosure(SensitiveBaseModel):
    """The full route data-sharing preview (paths, not values)."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    registry_name: Optional[str] = None
    items: list[RouteDataDisclosureItem] = Field(default_factory=list)
    sensitive_items: list[str] = Field(default_factory=list)  # paths, not values


class RouteConsentDecision(SensitiveBaseModel):
    """Result of granting/denying route-specific sharing consent."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    consent_id: Optional[str] = None
    granted: bool
    excluded: bool = False
    decided_at: dt.datetime
    already_decided: bool = False
