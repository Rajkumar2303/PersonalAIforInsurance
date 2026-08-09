"""Human checkpoint controls (Issue #5).

Reusable control signals that future browser/voice agents (Issue #7 / #9) must
pause before. Distinguishes:

- ``requires_explicit_human_checkpoint``: the operation must pause for a human.
- ``must_not_automate``: the automation must NOT perform the action at all
  (signature / payment / purchase / binding / renewal / cancellation).

This module only defines/returns structured instructions - no browser action,
no quote terminal statuses (Issue #8).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict

from ..insurance.base import SensitiveBaseModel


class HumanCheckpointKind(StrEnum):
    IDENTITY_LOOKUP = "identity_lookup"
    CONSENT_ATTESTATION = "consent_attestation"
    APPLICATION_DECLARATION = "application_declaration"
    SIGNATURE = "signature"
    PAYMENT = "payment"
    PURCHASE = "purchase"
    POLICY_BINDING = "policy_binding"
    RENEWAL = "renewal"
    CANCELLATION = "cancellation"


class CheckpointRequirement(SensitiveBaseModel):
    """A structured, safe instruction for a future agent."""

    model_config = ConfigDict(extra="forbid")

    kind: HumanCheckpointKind
    label: str
    reason: str
    requires_explicit_human_checkpoint: bool = True
    must_not_automate: bool = False
