"""Durable evidence / audit / trace API (Issue #10, Prompt 1).

Read-only evidence surface. Every endpoint requires ``intake_session_id`` as a
query parameter (ownership boundary) and returns API-safe view models - never
raw persisted blobs, never applicant values.

Endpoints:
- ``GET /api/v1/evidence/plans/{plan_id}``
- ``GET /api/v1/evidence/routes/{planned_route_id}``
- ``GET /api/v1/evidence/attempts/{attempt_id}``
- ``GET /api/v1/evidence/attempts/{attempt_id}/quotes``
- ``GET /api/v1/evidence/{evidence_id}``
- ``GET /api/v1/evidence/audit``
- ``GET /api/v1/evidence/export``

Evidence WRITE is explicit and adapter-driven (engine executors do not
auto-emit in Prompt 1); there is deliberately no generic write endpoint so the
only path to persist evidence is the validated EvidenceService adapters.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ConfigDict

from ..core.config import Settings, get_settings
from ..models.evidence import (
    AuditEventView,
    EvidenceExportView,
    EvidenceRecordView,
    QuoteObservationView,
)
from ..models.insurance.base import SensitiveBaseModel
from ..services.evidence import EvidenceService, get_evidence_sink, get_evidence_service
from ..services.evidence.service import _audit_view, _quote_view, _record_view

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])

logger = logging.getLogger(__name__)


class EvidenceHealthView(SensitiveBaseModel):
    """Minimal safe evidence health metadata (Prompt 2)."""

    model_config = ConfigDict(extra="forbid")

    evidence_status: str  # durable | persistence_failed | disabled
    evidence_backend: str  # in_memory | postgres | disabled


def _service(
    settings: Settings = Depends(get_settings),
    service: EvidenceService = Depends(get_evidence_service),
) -> EvidenceService:
    return service


@router.get("/health", response_model=EvidenceHealthView)
async def evidence_health(
    settings: Settings = Depends(get_settings),
    sink=Depends(get_evidence_sink),
) -> EvidenceHealthView:
    """Minimal health metadata for the future orchestrator/dashboard.

    ``evidence_status`` is durable unless the most recent write failed;
    ``evidence_backend`` reflects the configured repository backend. Never
    exposes applicant data.
    """
    if not sink.enabled:
        backend = "disabled"
        status = "disabled"
    else:
        backend = settings.evidence_repository_backend
        status = sink.evidence_status()
    return EvidenceHealthView(evidence_status=status, evidence_backend=backend)


@router.get("/plans/{plan_id}", response_model=list[EvidenceRecordView])
async def list_plan_evidence(
    plan_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: EvidenceService = Depends(_service),
) -> list[EvidenceRecordView]:
    records = await service.list_by_plan(intake_session_id, plan_id)
    return [_record_view(r) for r in records]


@router.get("/routes/{planned_route_id}", response_model=list[EvidenceRecordView])
async def list_route_evidence(
    planned_route_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: EvidenceService = Depends(_service),
) -> list[EvidenceRecordView]:
    records = await service.list_by_route(intake_session_id, planned_route_id)
    return [_record_view(r) for r in records]


@router.get("/attempts/{attempt_id}", response_model=list[EvidenceRecordView])
async def list_attempt_evidence(
    attempt_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: EvidenceService = Depends(_service),
) -> list[EvidenceRecordView]:
    records = await service.list_by_attempt(intake_session_id, attempt_id)
    return [_record_view(r) for r in records]


@router.get("/attempts/{attempt_id}/quotes", response_model=list[QuoteObservationView])
async def list_attempt_quotes(
    attempt_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: EvidenceService = Depends(_service),
) -> list[QuoteObservationView]:
    quotes = await service.list_quote_observations(intake_session_id, attempt_id)
    return [_quote_view(q) for q in quotes]


# NOTE: static paths (/audit, /export) are declared BEFORE the catch-all
# /{evidence_id} so Starlette routes them correctly.
@router.get("/audit", response_model=list[AuditEventView])
async def list_audit(
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: EvidenceService = Depends(_service),
) -> list[AuditEventView]:
    events = await service.list_audit_events(intake_session_id)
    return [_audit_view(e) for e in events]


@router.get("/export", response_model=EvidenceExportView)
async def export_evidence(
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: EvidenceService = Depends(_service),
) -> EvidenceExportView:
    return await service.export(intake_session_id)


@router.get("/{evidence_id}", response_model=EvidenceRecordView)
async def get_evidence(
    evidence_id: str,
    intake_session_id: str = Query(..., description="Ownership scope (required)"),
    service: EvidenceService = Depends(_service),
) -> EvidenceRecordView:
    record = await service.get(intake_session_id, evidence_id)
    if record is None:
        raise HTTPException(status_code=404, detail="evidence record not found")
    return _record_view(record)
