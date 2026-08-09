"""Intake models (Issue #5): field catalog, sessions, consent, disclosures,
checkpoints, and the safe workflow state."""

from __future__ import annotations

from .checkpoints import CheckpointRequirement, HumanCheckpointKind
from .consent import ConsentReceipt, ConsentScope, RouteDisclosureConsent
from .field_catalog import (
    CollectionGroup,
    FieldSensitivity,
    InputType,
    IntakeFieldDefinition,
    IntakePhase,
)
from .route import RouteConsentDecision, RouteDataDisclosure, RouteDataDisclosureItem
from .session import (
    FieldRequestOutcome,
    FieldRequestState,
    IntakeSession,
    IntakeSessionStatus,
    ProductGateResult,
    ProfileSummary,
    ProfileSummaryField,
    SafeQuestion,
    SubmitAnswerResult,
)
from .workflow import IntakeWorkflowState

__all__ = [
    "IntakeFieldDefinition",
    "InputType",
    "FieldSensitivity",
    "CollectionGroup",
    "IntakePhase",
    "IntakeSession",
    "IntakeSessionStatus",
    "FieldRequestState",
    "ProductGateResult",
    "SafeQuestion",
    "SubmitAnswerResult",
    "FieldRequestOutcome",
    "ProfileSummary",
    "ProfileSummaryField",
    "ConsentReceipt",
    "ConsentScope",
    "RouteDisclosureConsent",
    "RouteDataDisclosure",
    "RouteDataDisclosureItem",
    "RouteConsentDecision",
    "HumanCheckpointKind",
    "CheckpointRequirement",
    "IntakeWorkflowState",
]
