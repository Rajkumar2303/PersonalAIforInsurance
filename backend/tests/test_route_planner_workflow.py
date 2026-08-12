"""Tests for the LangGraph route-planner workflow (Issue #6).

The graph carries SAFE METADATA ONLY (counts + registry ids) - never applicant
values. The full RoutePlan is produced by the planner service, not placed in
traced state.
"""

from __future__ import annotations

import json

from app.graph.route_planner_workflow import WORKFLOW_NAME, build_route_planner_workflow
from app.models.insurance.enums import InsuranceType
from app.services.intake.engine import SessionNotFoundError

from route_planner_helpers import StubProfileSource, entry, make_planner


class MissingSessionSource(StubProfileSource):
    def get_session(self, session_id: str):
        raise SessionNotFoundError(session_id)


def test_plan_routes_produces_safe_state(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [
            entry("ready-a", distinct_rate_source_id="RS-1"),
            entry("blocked-b", distinct_rate_source_id="RS-2"),
        ],
        rate_sources=[
            {"distinct_rate_source_id": "RS-1", "product_type": "auto", "related_registry_ids": ["ready-a"]},
            {"distinct_rate_source_id": "RS-2", "product_type": "auto", "related_registry_ids": ["blocked-b"]},
        ],
        profile_source=StubProfileSource(
            presence={p: True for p in [
                "applicant.identity.legal_name",
                "applicant.address.postal_code",
                "product_data.drivers[0].licence.licence_number",
                "product_data.vehicles[0].identity.vin",
            ]},
            consent={"ready-a": True, "blocked-b": False},
        ),
    )
    graph = build_route_planner_workflow(planner)
    result = graph.invoke({"entry": "plan", "session_id": "session-1"})
    assert result.get("workflow_status") == "complete"
    assert result.get("planned_route_count") == 2
    assert result.get("ready_route_count") == 1
    assert result.get("blocked_route_count") == 1
    assert result.get("ready_registry_ids") == ["ready-a"]
    assert result.get("missing_field_path_count") == 0


def test_state_contains_safe_metadata_only(tmp_path) -> None:
    planner = make_planner(tmp_path, [entry("route-a")])
    graph = build_route_planner_workflow(planner)
    result = graph.invoke({"entry": "plan", "session_id": "session-1"})
    text = json.dumps(result, default=str)
    for marker in ("T0000-00000-00000", "1HGCM82633A000000", "M0A 0A0", "Test Applicant", "1990-01-01"):
        assert marker not in text


def test_product_gate_non_auto_ends(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        profile_source=StubProfileSource(insurance_type=InsuranceType.HOME),
    )
    graph = build_route_planner_workflow(planner)
    result = graph.invoke({"entry": "plan", "session_id": "session-home"})
    assert result.get("workflow_status") == "product_not_applicable"


def test_session_not_found_safe(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a")],
        profile_source=MissingSessionSource(),
    )
    graph = build_route_planner_workflow(planner)
    result = graph.invoke({"entry": "plan", "session_id": "nope"})
    assert result.get("workflow_status") == "session_not_found"


def test_workflow_name_is_stable() -> None:
    assert WORKFLOW_NAME == "route_planner_workflow"
