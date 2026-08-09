"""Route planner services (Issue #6): requirement resolver + deterministic
planner."""

from __future__ import annotations

from typing import Optional

from .planner import IntakeProfileSource, RoutePlanner, RoutePlannerProfileSource
from .requirements import RequirementResolver

__all__ = [
    "RequirementResolver",
    "RoutePlanner",
    "RoutePlannerProfileSource",
    "IntakeProfileSource",
    "get_route_planner",
]

_planner: Optional[RoutePlanner] = None


def get_route_planner() -> RoutePlanner:
    """Cached default planner (real registry + dedup + data requirements)."""
    global _planner
    if _planner is None:
        _planner = RoutePlanner()
    return _planner
