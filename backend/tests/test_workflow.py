"""Tests for the minimal LangGraph workflow and its tracing config."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.tracing import run_config
from app.graph.workflow import WORKFLOW_NAME, build_demo_workflow


def test_demo_workflow_executes_sync() -> None:
    """The compiled graph runs both nodes in order (sync invoke)."""
    graph = build_demo_workflow()
    result = graph.invoke({"input_text": "hello"})
    assert result["steps"] == ["stage_one", "stage_two"]
    assert result["stage"] == "stage_two"
    assert result["final_output"] == "processed: hello"


async def test_demo_workflow_executes_async() -> None:
    """The compiled graph runs both nodes in order (async ainvoke)."""
    graph = build_demo_workflow()
    result = await graph.ainvoke({"input_text": "world"})
    assert result["steps"] == ["stage_one", "stage_two"]
    assert result["final_output"] == "processed: world"


def test_workflow_compiles() -> None:
    """The graph is a compiled StateGraph with the expected node names."""
    graph = build_demo_workflow()
    nodes = set(graph.get_graph().nodes.keys())
    assert {"stage_one", "stage_two"} <= nodes


def test_run_config_metadata_is_non_sensitive() -> None:
    """Tracing metadata/tags carry identifiers, never applicant data."""
    settings = get_settings()
    config = run_config(settings, request_id="req-123", workflow=WORKFLOW_NAME)

    assert config["metadata"]["environment"] == settings.app_env
    assert config["metadata"]["workflow"] == WORKFLOW_NAME
    assert config["metadata"]["request_id"] == "req-123"
    assert "ontario-allquote-agent" in config["tags"]
    assert WORKFLOW_NAME in config["tags"]


def test_run_config_sets_run_id_from_request_id() -> None:
    """The LangSmith root run id defaults to the request id for correlation."""
    settings = get_settings()
    config = run_config(settings, request_id="a3ca60465eba4b9d8b52342bb299c409")
    assert str(config["run_id"]) == "a3ca60465eba-4b9d-8b52-342bb299c409" or (
        config["run_id"].hex == "a3ca60465eba4b9d8b52342bb299c409"
    )


def test_run_config_allows_explicit_run_id() -> None:
    """An explicit run_id overrides the request id."""
    settings = get_settings()
    config = run_config(
        settings,
        request_id="a3ca60465eba4b9d8b52342bb299c409",
        run_id="12345678-1234-5678-1234-567812345678",
    )
    assert str(config["run_id"]) == "12345678-1234-5678-1234-567812345678"


def test_demo_endpoint_runs_workflow(client: TestClient) -> None:
    """POST /api/v1/demo/workflow executes the graph and returns a request_id."""
    response = client.post("/api/v1/demo/workflow", json={"input_text": "hello"})
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "success"
    assert body["workflow"] == WORKFLOW_NAME
    assert body["stages"] == ["stage_one", "stage_two"]
    assert body["final_output"] == "processed: hello"
    assert body["request_id"]
