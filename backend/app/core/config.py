"""Centralized application configuration.

All settings are read from environment variables (optionally via a `.env`
file at the repository root). No secrets are hardcoded or committed; see
`.env.example` at the repo root for the full set of placeholders.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> repository root is 4 levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Values come from (highest priority first): process environment, the
    repo-root `.env` file, then the defaults declared below.
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ------------------------------------------------
    app_env: str = "development"  # development | test | production
    app_name: str = "Ontario All-Quote Agent"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- CORS -------------------------------------------------------
    # Comma-separated list of allowed frontend origins.
    frontend_origins: str = "http://localhost:5173"

    # --- LangSmith (observability) ----------------------------------
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "ontario-allquote-agent"
    langsmith_endpoint: str | None = None

    # --- Database (placeholder — NOT used in Issue 1) ---------------
    database_url: str | None = None

    # --- LLM (placeholder — NOT used in Issue 1) --------------------
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None

    @property
    def cors_origins(self) -> list[str]:
        """Parsed list of allowed CORS origins."""
        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
