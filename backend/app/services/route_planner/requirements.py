"""Data-driven route requirement resolver (Issue #6).

A route's required canonical field paths come from DATA, never code:

- ``data/routes/auto_route_requirements.json``: a ``default`` list applied to
  every AUTO route plus a ``per_route`` map for specific registry ids.
- A deterministic mapping of the registry's ``MarketRequirement`` enum
  (LICENCE / VIN) to canonical paths.

This keeps requirements and market changes data-driven and avoids any
insurer-specific if/elif logic (Issue #6 rules 6-7).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from ...core.config import BACKEND_ROOT, get_settings
from ...models.registry import MarketRegistryEntry, MarketRequirement

logger = logging.getLogger(__name__)

# Deterministic mapping: registry MarketRequirement -> canonical field paths.
MARKET_REQUIREMENT_PATHS: dict[MarketRequirement, list[str]] = {
    MarketRequirement.LICENCE: ["product_data.drivers[0].licence.licence_number"],
    MarketRequirement.VIN: ["product_data.vehicles[0].identity.vin"],
}

_REQUIREMENTS_FILE = "auto_route_requirements.json"


class RequirementsLoadError(RuntimeError):
    """Raised when the requirements data file is invalid."""


def default_requirements_dir() -> Path:
    """Resolve the route-requirements data directory (CWD-independent)."""
    settings = get_settings()
    if settings.route_requirements_dir:
        return Path(settings.route_requirements_dir)
    return BACKEND_ROOT / "data" / "routes"


class RequirementResolver:
    """Deterministic, data-driven per-route requirement resolution."""

    def __init__(self, requirements_dir: Optional[Path] = None) -> None:
        self._dir = Path(requirements_dir) if requirements_dir else default_requirements_dir()
        self._default: list[str] = []
        self._per_route: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        path = self._dir / _REQUIREMENTS_FILE
        if not path.exists():
            logger.warning(
                "route requirements file not found",
                extra={"workflow": "route_planner", "workflow_stage": "requirements_load", "status": "missing"},
            )
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RequirementsLoadError(f"failed to read route requirements {path.name}") from exc
        self._default = list(raw.get("default", []) or [])
        self._per_route = {str(k): list(v or []) for k, v in (raw.get("per_route", {}) or {}).items()}

    def requirements_for(self, entry: MarketRegistryEntry) -> set[str]:
        """Required canonical field paths for a registry entry (data + enum map)."""
        paths: set[str] = set(self._default)
        paths.update(self._per_route.get(entry.registry_id, []))
        for requirement in entry.requirements:
            paths.update(MARKET_REQUIREMENT_PATHS.get(requirement, []))
        return paths

    def trace_metadata(self) -> dict[str, object]:
        """Safe counts for logs/traces."""
        return {
            "default_requirement_count": len(self._default),
            "per_route_override_count": len(self._per_route),
        }
