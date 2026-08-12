"""Privacy tests for the route planner (Issue #6).

Synthetic sensitive values must never appear in the RoutePlan, the LangGraph
state, or structured logs - the planner works only with presence booleans and
canonical paths.
"""

from __future__ import annotations

import json
import logging

from app.graph.route_planner_workflow import build_route_planner_workflow

from route_planner_helpers import StubProfileSource, entry, make_planner

SENSITIVE_MARKERS = [
    "T0000-00000-00000",
    "1HGCM82633A000000",
    "M0A 0A0",
    "416-555-0199",
    "test.applicant@example.com",
    "1990-01-01",
    "Test Applicant",
    "synthetic minor collision at low speed",
]


def test_plan_has_no_sensitive_values(tmp_path) -> None:
    planner = make_planner(
        tmp_path,
        [entry("route-a", quote_url="https://example.test/quote", public_phone_route="1-800-555-0199")],
    )
    plan = planner.plan("session-1")
    text = json.dumps(plan.model_dump(mode="json"))
    for marker in SENSITIVE_MARKERS:
        assert marker not in text


def test_graph_state_has_no_sensitive_values(tmp_path) -> None:
    planner = make_planner(tmp_path, [entry("route-a")])
    graph = build_route_planner_workflow(planner)
    result = graph.invoke({"entry": "plan", "session_id": "session-1"})
    text = json.dumps(result, default=str)
    for marker in SENSITIVE_MARKERS:
        assert marker not in text


def test_planner_logs_are_safe(tmp_path, caplog) -> None:
    planner = make_planner(tmp_path, [entry("route-a")])
    with caplog.at_level(logging.INFO):
        planner.plan("session-1")
    for marker in SENSITIVE_MARKERS:
        assert marker not in caplog.text


def test_plan_repr_is_redacted(tmp_path) -> None:
    planner = make_planner(tmp_path, [entry("route-a")])
    plan = planner.plan("session-1")
    for marker in SENSITIVE_MARKERS:
        assert marker not in repr(plan)
