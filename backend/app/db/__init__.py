"""Database engine/session helpers (Issue #10, Prompt 1).

Production uses PostgreSQL via ``postgresql+asyncpg://...``. Hermetic tests use
SQLite via ``sqlite+aiosqlite://`` with the SAME SQLAlchemy models (dialect
portable: JSON columns, String enums, Numeric money). No global mutable engine
singleton is required - callers build engines explicitly; the default evidence
service stays on the in-memory repository unless ``database_url`` is configured.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

__all__ = ["create_evidence_engine", "evidence_session_factory", "AsyncEngine", "AsyncSession"]


def create_evidence_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine for a URL (asyncpg / aiosqlite)."""
    return create_async_engine(database_url, pool_pre_ping=True)


def evidence_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an async session factory bound to the engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def default_evidence_database_url() -> Optional[str]:
    """The configured Postgres URL, or None when evidence should stay in-memory."""
    from ..core.config import get_settings

    settings = get_settings()
    if settings.evidence_repository_backend == "postgres" and settings.database_url:
        return settings.database_url
    return None
