"""Tests for centralized configuration loading."""

from __future__ import annotations

from app.core.config import Settings, get_settings


def test_defaults(monkeypatch) -> None:
    """Settings expose sensible defaults and no hardcoded secrets.

    ``_env_file=None`` ignores any repo-root ``.env``, and the LangSmith vars
    are cleared from the process environment so the test only relies on the
    declared defaults.
    """
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 8000
    assert settings.langsmith_tracing is False
    assert settings.langsmith_project == "ontario-allquote-agent"
    assert settings.database_url is None
    assert settings.llm_api_key is None
    assert settings.cors_origins == ["http://localhost:5173"]


def test_env_override(monkeypatch) -> None:
    """Environment variables override defaults."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("API_PORT", "9999")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:3000, http://localhost:5173")

    settings = Settings()
    assert settings.app_env == "test"
    assert settings.api_port == 9999
    assert settings.langsmith_tracing is True
    assert settings.cors_origins == ["http://localhost:3000", "http://localhost:5173"]


def test_cors_origins_parsed() -> None:
    """CORS origins are parsed and whitespace-trimmed."""
    settings = Settings(frontend_origins="http://a,http://b , http://c")
    assert settings.cors_origins == ["http://a", "http://b", "http://c"]


def test_get_settings_is_cached() -> None:
    """The settings singleton is cached."""
    assert get_settings() is get_settings()
