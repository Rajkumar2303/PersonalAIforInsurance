"""Route identity mapping for the browser layer (Issue #7).

``planned_route_id`` is the route identity used by the BrowserSession/API.
Today it is mapped 1:1 to the registry_id. This is a TEMPORARY compatibility
mapping only - it is centralized HERE so that when the route planner gains a
real ``planned_route_id`` (distinct from registry_id), only this module
changes. Do NOT treat ``registry_id == planned_route_id`` as a permanent
invariant scattered across the codebase.
"""

from __future__ import annotations


def registry_id_for_planned_route(planned_route_id: str) -> str:
    """Resolve the registry_id for a planned_route_id (compat shim)."""
    return planned_route_id


def planned_route_id_for_registry(registry_id: str) -> str:
    """Resolve the planned_route_id for a registry_id (compat shim)."""
    return registry_id
