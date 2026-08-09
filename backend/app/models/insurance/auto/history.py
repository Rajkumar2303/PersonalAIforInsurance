"""Insurance and driving history models (Ontario auto).

All free-text detail/description fields are treated as sensitive and are
redacted in safe output (see ``base.SENSITIVE_FIELD_NAMES``).
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import Field

from ..base import SensitiveBaseModel


class CurrentInsurance(SensitiveBaseModel):
    current_insurer: Optional[str] = None
    policy_number: Optional[str] = None  # SENSITIVE: redacted
    expiry_date: Optional[dt.date] = None
    current_premium: Optional[float] = Field(default=None, ge=0)
    years_continuously_insured: Optional[float] = Field(default=None, ge=0)
    reason_for_shopping: Optional[str] = None


class LicenceEvent(SensitiveBaseModel):
    """Suspension/cancellation or other licence events."""

    event_date: dt.date
    details: Optional[str] = None  # SENSITIVE: redacted


class CancellationEvent(SensitiveBaseModel):
    """Insurance cancellations within the relevant lookback period."""

    cancellation_date: dt.date
    reason: Optional[str] = None


class MisrepresentationRecord(SensitiveBaseModel):
    """Relevant policy cancellation or claim denial due to misrepresentation."""

    policy_cancellation_or_claim_denial: bool = False
    event_date: Optional[dt.date] = None
    details: Optional[str] = None  # SENSITIVE: redacted


class FraudFinding(SensitiveBaseModel):
    court_finding: bool = False
    finding_date: Optional[dt.date] = None
    details: Optional[str] = None  # SENSITIVE: redacted


class ClaimRecord(SensitiveBaseModel):
    """An accident or claim event. SENSITIVE: free-text details redacted."""

    driver_reference: Optional[str] = None
    vehicle_reference: Optional[str] = None
    claim_date: dt.date
    fault_percentage: Optional[float] = Field(default=None, ge=0, le=100)
    coverage: Optional[str] = None
    paid_or_estimated_amount: Optional[float] = Field(default=None, ge=0)
    details: Optional[str] = None  # SENSITIVE: redacted


class ConvictionRecord(SensitiveBaseModel):
    driver_reference: Optional[str] = None
    conviction_date: dt.date
    description: Optional[str] = None  # SENSITIVE: redacted


class InsuranceAndDrivingHistory(SensitiveBaseModel):
    current_insurance: CurrentInsurance = Field(default_factory=CurrentInsurance)
    licence_events: list[LicenceEvent] = Field(default_factory=list)
    cancellations: list[CancellationEvent] = Field(default_factory=list)
    misrepresentation: list[MisrepresentationRecord] = Field(default_factory=list)
    fraud_findings: list[FraudFinding] = Field(default_factory=list)
    accidents_and_claims: list[ClaimRecord] = Field(default_factory=list)
    convictions: list[ConvictionRecord] = Field(default_factory=list)
