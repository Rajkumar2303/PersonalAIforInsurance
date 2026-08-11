"""Evidence service package (Issue #10, Prompt 1).

Default is the hermetic in-memory repository. When ``database_url`` is set and
``evidence_repository_backend=postgres``, a shared async engine + SQLAlchemy
repository is used. Tests construct ``EvidenceService`` directly with an
in-memory or a SQLite-backed repository.

Prompt 2 adds the ``EvidenceSink`` surface used by engines for AUTOMATIC
evidence emission (``get_evidence_sink``), with a safe no-op default.
"""

from __future__ import annotations

from functools import lru_cache

from ...db import create_evidence_engine
from .persistence import SqlAlchemyEvidenceRepository
from .repository import InMemoryEvidenceRepository
from .service import EvidenceService
from .sink import EvidenceSink, EvidenceServiceSink, NoopEvidenceSink


def _build_service() -> EvidenceService:
    from ...core.config import get_settings

    settings = get_settings()
    if settings.evidence_repository_backend == "postgres" and settings.database_url:
        engine = create_evidence_engine(settings.database_url)
        return EvidenceService(SqlAlchemyEvidenceRepository(engine))
    return EvidenceService(InMemoryEvidenceRepository())


@lru_cache(maxsize=1)
def get_evidence_service() -> EvidenceService:
    """Process-wide evidence service singleton (API/demo wiring)."""
    return _build_service()


@lru_cache(maxsize=1)
def get_evidence_sink() -> EvidenceServiceSink:
    """Shared synchronous evidence sink for automatic engine emission."""
    return EvidenceServiceSink(get_evidence_service())


__all__ = [
    "EvidenceService",
    "EvidenceSink",
    "EvidenceServiceSink",
    "NoopEvidenceSink",
    "InMemoryEvidenceRepository",
    "SqlAlchemyEvidenceRepository",
    "get_evidence_service",
    "get_evidence_sink",
]
