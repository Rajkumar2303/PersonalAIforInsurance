"""Action safety classification (Issue #7).

Before any click/submission the executor classifies the action:

- ``safe_navigation`` / ``data_submission``: allowed (quote-form flow).
- ``human_checkpoint``: must pause for a human (identity, declaration, ...).
- ``prohibited``: must NEVER be automated (signature/payment/purchase/binding/
  renewal/cancellation).

Classification is deterministic, config-driven (checkpoint + action bindings)
and reuses Issue #5 ``CheckpointService`` semantics. Unclassified buttons are
NOT auto-clicked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..models.browser.config import BrowserRouteConfig
from ..models.browser.session import BrowserActionSafety
from ..models.intake.checkpoints import CheckpointRequirement, HumanCheckpointKind
from ..services.intake.checkpoints import CheckpointService


@dataclass
class Clickable:
    """A clickable control and its deterministic safety classification."""

    label: str
    action_type: str
    safety: Optional[BrowserActionSafety]  # None = unknown (do not auto-click)
    checkpoint: Optional[CheckpointRequirement] = None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class ActionClassifier:
    """Deterministic safety classification of clickable actions."""

    def __init__(self, checkpoint_service: Optional[CheckpointService] = None) -> None:
        self._checkpoints = checkpoint_service or CheckpointService()

    def classify(self, text: str, config: BrowserRouteConfig) -> Clickable:
        normalized = _normalize(text)
        # 1) checkpoint bindings take precedence (safety boundaries).
        for binding in config.checkpoint_bindings:
            if not binding.enabled:
                continue
            if any(_normalize(p) in normalized for p in binding.label_patterns):
                return self._checkpoint_result(text, binding.checkpoint_type)
        # 2) known navigation/data-submission actions.
        for binding in config.action_bindings:
            if not binding.enabled:
                continue
            if any(_normalize(p) in normalized for p in binding.label_patterns):
                return Clickable(label=text, action_type=binding.action_type, safety=binding.safety)
        # 3) unknown - never auto-click.
        return Clickable(label=text, action_type="unknown", safety=None)

    def _checkpoint_result(self, label: str, checkpoint_type: str) -> Clickable:
        try:
            kind = HumanCheckpointKind(checkpoint_type)
        except ValueError:
            return Clickable(
                label=label,
                action_type=checkpoint_type,
                safety=BrowserActionSafety.HUMAN_CHECKPOINT,
            )
        requirement = self._checkpoints.evaluate(kind)
        if requirement is not None and requirement.must_not_automate:
            return Clickable(label=label, action_type=kind.value, safety=BrowserActionSafety.PROHIBITED, checkpoint=requirement)
        return Clickable(label=label, action_type=kind.value, safety=BrowserActionSafety.HUMAN_CHECKPOINT, checkpoint=requirement)
