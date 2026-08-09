"""FastAPI application factory for the Ontario All-Quote Agent backend."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.browser import router as browser_router
from .api.demo import router as demo_router
from .api.dedup import router as dedup_router
from .api.health import router as health_router
from .api.intake import router as intake_router
from .api.markets import router as markets_router
from .api.planner import router as planner_router
from .core.config import Settings, get_settings
from .core.logging import setup_logging
from .core.tracing import configure_tracing

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the FastAPI application with tracing, logging, CORS, and routes."""
    settings = get_settings()

    # Observability wiring (idempotent, safe with no LangSmith credentials).
    configure_tracing(settings)
    setup_logging()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("backend starting", extra={"workflow": "startup", "workflow_stage": "boot"})
        yield
        logger.info("backend stopped", extra={"workflow": "startup", "workflow_stage": "shutdown"})

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Evidence-first Ontario auto-insurance shopping assistant "
            "(Issue 1: project setup, architecture & observability)."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(demo_router)
    app.include_router(markets_router)
    app.include_router(dedup_router)
    app.include_router(intake_router)
    app.include_router(planner_router)
    app.include_router(browser_router)

    return app


app = create_app()
