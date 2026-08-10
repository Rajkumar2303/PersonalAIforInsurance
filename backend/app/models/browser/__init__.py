"""Browser domain models (Issue #7).

These models describe the browser execution layer for one READY web route on
one intake session. They carry SAFE METADATA ONLY - ids, canonical field paths,
counts, page signatures, action/observation/checkpoint types. They NEVER carry
applicant field values (licence, DOB, VIN, address, claims, phone/email).

Quote/reference identifiers are user-specific: the raw reference never leaves a
private boundary. ``RawQuoteObservation`` exposes only ``reference_present``
plus an opaque ``private_reference_handle``.
"""

from __future__ import annotations

from .config import BrowserRouteConfig
from .observation import (
    BrowserCheckpointObservation,
    BrowserFieldObservation,
    BrowserObservation,
    BrowserObservationType,
    BrowserPageObservation,
    BrowserQuoteObservation,
    RawQuoteObservation,
)
from .session import (
    BrowserActionSafety,
    BrowserActionResult,
    BrowserExecutionMode,
    BrowserRefusalReason,
    BrowserSession,
    BrowserSessionStatus,
    BrowserStartRefusal,
    BrowserStepResult,
    LiveExecutionGate,
)
from .workflow import BrowserWorkflowState

__all__ = [
    "BrowserActionSafety",
    "BrowserActionResult",
    "BrowserExecutionMode",
    "BrowserRefusalReason",
    "BrowserSession",
    "BrowserSessionStatus",
    "BrowserStartRefusal",
    "BrowserStepResult",
    "LiveExecutionGate",
    "BrowserCheckpointObservation",
    "BrowserFieldObservation",
    "BrowserObservation",
    "BrowserObservationType",
    "BrowserPageObservation",
    "BrowserQuoteObservation",
    "RawQuoteObservation",
    "BrowserRouteConfig",
    "BrowserWorkflowState",
]
