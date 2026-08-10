"""Data-driven recovery policy loader (Issue #8).

Safe recovery preferences live in DATA (``data/recovery/auto_policy.json``),
not code, so behavior changes are config/data-only and never require a
``RecoveryEngine`` modification.

HARD SAFETY BOUNDARIES ARE NOT EDITABLE WEIGHTS: CAPTCHA bypass / bot-control
bypass / consent bypass / purchase automation remain prohibited regardless of
any policy value.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from ...core.config import BACKEND_ROOT, get_settings
from ...models.recovery import RecoveryPolicy

logger = logging.getLogger(__name__)

_POLICY_FILE = "auto_policy.json"


class RecoveryPolicyLoadError(RuntimeError):
    """Raised when the recovery policy data file is invalid."""


def default_policy_dir() -> Path:
    """Resolve the recovery-policy data directory (CWD-independent)."""
    settings = get_settings()
    if settings.recovery_policy_dir:
        return Path(settings.recovery_policy_dir)
    return BACKEND_ROOT / "data" / "recovery"


class RecoveryPolicyLoader:
    """Deterministic, data-driven recovery policy."""

    def __init__(self, policy_dir: Optional[Path] = None) -> None:
        self._dir = Path(policy_dir) if policy_dir else default_policy_dir()
        self._default: dict = {}
        self._version: Optional[str] = None
        self._load()

    def _load(self) -> None:
        path = self._dir / _POLICY_FILE
        if not path.exists():
            logger.warning(
                "recovery policy file not found - using conservative defaults",
                extra={"workflow": "recovery", "workflow_stage": "policy_load", "status": "missing"},
            )
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryPolicyLoadError(f"failed to read recovery policy {path.name}") from exc
        self._version = raw.get("version")
        self._default = dict(raw.get("default", {}) or {})

    def load(self) -> RecoveryPolicy:
        """Return the policy with data overrides on top of conservative defaults."""
        return RecoveryPolicy(**self._default, version=self._version or self._default.get("version"))

    def version(self) -> Optional[str]:
        """Policy version from data (audit provenance)."""
        return self._version

    def trace_metadata(self) -> dict[str, object]:
        """Safe counts for logs/traces."""
        return {"recovery_policy_keys": len(self._default), "recovery_policy_version": self._version}
