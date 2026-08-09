"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Keep automated tests hermetic: never upload traces to the real LangSmith
# project during the test run, regardless of local `.env` settings.
os.environ["LANGSMITH_TRACING"] = "false"

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    """A TestClient wrapping the FastAPI application."""
    with TestClient(create_app()) as test_client:
        yield test_client
