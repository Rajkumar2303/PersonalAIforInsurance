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
BACKEND_ROOT = REPO_ROOT / "backend"


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

    # --- Market registry (Issue #3) ---------------------------------
    # Optional override for the data-driven registry dir; defaults to
    # BACKEND_ROOT/data/market_registry (see app/services/market_registry.py).
    market_registry_dir: str | None = None

    # --- Rate sources (Issue #4) ------------------------------------
    # Optional override for the deduplication rate-source data dir; defaults to
    # BACKEND_ROOT/data/rate_sources (see app/services/deduplication.py).
    rate_sources_dir: str | None = None

    # --- Intake (Issue #5) ------------------------------------------
    # Optional override for the data-driven intake field catalog dir; defaults
    # to BACKEND_ROOT/data/intake (see app/services/intake/catalog.py).
    intake_catalog_dir: str | None = None
    # Where an encrypted profile vault persists its files (when used); the
    # directory is gitignored. Defaults to BACKEND_ROOT/data/vault.
    intake_vault_dir: str | None = None
    # Encryption key for encrypted profile vaults. MUST come from the
    # environment/.env only - never hardcoded or committed. When unset, the
    # encrypted vault cannot be created (in-memory vault is used instead).
    intake_vault_key: str | None = None

    # --- Route planner (Issue #6) ------------------------------------
    # Optional override for the data-driven route-requirements dir; defaults to
    # BACKEND_ROOT/data/routes (see app/services/route_planner/requirements.py).
    route_requirements_dir: str | None = None

    # --- Browser agent (Issue #7) ------------------------------------
    # Optional override for the data-driven browser route-config dir; defaults
    # to BACKEND_ROOT/data/browser/routes (see app/browser/config.py).
    browser_route_config_dir: str | None = None
    # Headless vs headful Chromium. Headless is the hermetic-test default;
    # pass False (headful) for manual/demo observation.
    browser_headless: bool = True
    # DEV/DEMO ONLY: Playwright slow_mo delay (ms) between browser actions so a
    # headful demo is easy to watch. Default 0 = no delay in tests/production.
    browser_slow_mo_ms: int = 0
    # When True, LIVE browser execution requires the explicit personal-use gate
    # (personal_use_confirmed + accurate_information_attested + route consent).
    browser_live_gate_required: bool = True
    # Screenshots are optional and must be redacted before persistence; live
    # mode disables them by default.
    browser_screenshot_enabled: bool = False
    # Safety bound on the number of browser steps per run (never an infinite
    # autonomous loop).
    browser_max_steps: int = 20
    # Abandoned in-memory browser sessions are closed after this many seconds.
    browser_idle_timeout_seconds: int = 600

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
