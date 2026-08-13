"""Privacy-safe browser action event (browser-action logging).

One safe, structured event for a single deterministic browser action. Carries
ONLY correlation + classification metadata: ``provider`` (registry id), the
canonical_field PATH (never the value), the action category, and a status.

It deliberately has NO value, selector, page-text, URL-query, cookie, token,
screenshot, or raw-Playwright-log field — ``extra="forbid"`` means applicant
data can never cross this model. Events are emitted by the generic browser
executor and (when a sink is wired) preserved as redacted
``field_interaction_observed`` evidence records.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import ConfigDict, Field

from ..insurance.base import SensitiveBaseModel

# Canonical action categories (never free-form).
NAVIGATE = "navigate"
FILL = "fill"
SELECT = "select"
CLICK = "click"
PAUSE = "pause"
EXTRACT = "extract"

# Status values (safe).
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_PAUSED = "paused"
STATUS_BLOCKED = "blocked"
STATUS_SKIPPED = "skipped"


class BrowserActionEvent(SensitiveBaseModel):
    """One safe browser action (correlation + classification only)."""

    model_config = ConfigDict(extra="forbid")

    provider: str  # registry_id, e.g. "sonnet"
    action: str  # navigate | fill | select | click | pause | extract
    canonical_field: Optional[str] = None  # canonical PATH, never a value
    status: str = STATUS_SUCCESS  # success | failure | paused | blocked | skipped
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    attempt_id: Optional[str] = None
    plan_id: Optional[str] = None
    distinct_rate_source_id: Optional[str] = None
    browser_session_id: Optional[str] = None
    observed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
