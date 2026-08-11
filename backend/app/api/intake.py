"""Intake API (Issue #5).

Reusable consent-aware progressive intake endpoints that future browser (#7)
and voice (#9) agents will call. Responses contain SAFE data only - a profile
summary never echoes values, and question payloads expose metadata for one
field at a time.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.config import Settings, get_settings
from ..core.logging import clear_log_context, set_log_context
from ..core.tracing import run_config
from ..graph.intake_workflow import (
    WORKFLOW_NAME,
    build_intake_workflow,
    clear_pending_answer,
    set_pending_answer,
)
from ..models.intake.checkpoints import HumanCheckpointKind
from ..models.intake.consent import ConsentReceipt, ConsentScope
from ..models.intake.field_catalog import IntakeFieldDefinition
from ..models.intake.route import RouteConsentDecision, RouteDataDisclosure
from ..models.intake.session import (
    FieldRequestOutcome,
    IntakeSession,
    ProductGateResult,
    ProfileSummary,
    SafeQuestion,
    SubmitAnswerResult,
)
from ..models.insurance.enums import InsuranceType
from ..services.intake.catalog import IntakeFieldCatalog
from ..services.intake.engine import RouteNotFoundError, SessionNotFoundError
from ..services.intake import get_intake_engine

router = APIRouter(prefix="/api/v1", tags=["intake"])

logger = logging.getLogger(__name__)


# --- request/response models -------------------------------------------

class CreateSessionRequest(BaseModel):
    insurance_type: InsuranceType


class CreateSessionResponse(BaseModel):
    session: IntakeSession
    gate: ProductGateResult


class NextQuestionResponse(BaseModel):
    session_id: str
    workflow_status: str
    question: Optional[SafeQuestion] = None


class AnswerRequest(BaseModel):
    canonical_path: str
    value: Any


class RequestFieldsRequest(BaseModel):
    requested_paths: list[str]
    source_context: Optional[str] = None


class RouteDisclosureRequest(BaseModel):
    registry_id: str
    paths: Optional[list[str]] = None


class RouteConsentRequest(BaseModel):
    registry_id: str
    paths: list[str] = Field(default_factory=list)
    granted: bool


class ConsentRequest(BaseModel):
    scope: ConsentScope  # collection | household_driver
    driver_label: Optional[str] = None


class CatalogField(BaseModel):
    """One safe, data-driven catalog field definition (no values, no logic).

    ``canonical_path`` is the concrete path after index-0 template resolution.
    The frontend renders from this catalog rather than its own field schema, so
    adding/removing/renaming a field is a catalog change - never a rewrite.
    """

    field_id: str
    canonical_path: str
    question: str
    short_label: str
    input_type: str
    collection_group: str
    intake_phase: str
    sensitivity: str
    choices: list[str] = Field(default_factory=list)
    priority: int = 100
    seed_required: bool = False
    item_unit: Optional[str] = None
    item_unit_required: bool = False
    household_attestation_required: bool = False
    help_text: Optional[str] = None


def _404(detail: str) -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def _intake_engine_for_mode(mode: str):
    """Resolve the intake engine by execution mode (Issue #8.5).

    ``mock`` resolves the shared-store engine wired to the isolated demo
    overlay (so route disclosure + consent can reference synthetic mock
    routes without ever touching the real market registry). ``live`` (default)
    uses the real singleton - unchanged behavior.
    """
    if mode == "mock":
        from ..demo.runtime import get_demo_runtime

        return get_demo_runtime().intake
    return get_intake_engine()


# --- endpoints ----------------------------------------------------------

@router.post("/intake/sessions", response_model=CreateSessionResponse, summary="Start a product-aware intake session")
async def create_session(
    payload: CreateSessionRequest,
    settings: Settings = Depends(get_settings),
) -> CreateSessionResponse:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="create_session")
    try:
        engine = get_intake_engine()
        session, gate = engine.create_session(payload.insurance_type)
        return CreateSessionResponse(session=session, gate=gate)
    finally:
        clear_log_context()


@router.get("/intake/sessions/{session_id}", response_model=IntakeSession, summary="Get an intake session (safe metadata)")
async def get_session(session_id: str) -> IntakeSession:
    try:
        return get_intake_engine().get_session(session_id)
    except SessionNotFoundError:
        raise _404("intake session not found")


@router.get(
    "/intake/sessions/{session_id}/next-question",
    response_model=NextQuestionResponse,
    summary="Get the next question via the LangGraph advance flow",
)
async def next_question(
    session_id: str,
    settings: Settings = Depends(get_settings),
) -> NextQuestionResponse:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="advance")
    engine = get_intake_engine()
    try:
        engine.get_session(session_id)
    except SessionNotFoundError:
        clear_log_context()
        raise _404("intake session not found")
    try:
        config = run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            extra_metadata={"session_id": session_id, "workflow_stage": "advance"},
        )
        result = await build_intake_workflow(engine).ainvoke(
            {"entry": "advance", "session_id": session_id}, config=config
        )
        workflow_status = result.get("workflow_status", "intake_ready")
        field_id = result.get("current_field_id")
        question = engine.question_payload_for(field_id) if field_id else None
        return NextQuestionResponse(session_id=session_id, workflow_status=workflow_status, question=question)
    finally:
        clear_log_context()


@router.post(
    "/intake/sessions/{session_id}/answers",
    response_model=SubmitAnswerResult,
    summary="Submit one answer via the LangGraph submit flow",
)
async def submit_answer(
    session_id: str,
    payload: AnswerRequest,
    settings: Settings = Depends(get_settings),
) -> SubmitAnswerResult:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="submit")
    engine = get_intake_engine()
    try:
        engine.get_session(session_id)
    except SessionNotFoundError:
        clear_log_context()
        raise _404("intake session not found")
    set_pending_answer(session_id, payload.canonical_path, payload.value)
    try:
        config = run_config(
            settings,
            request_id=request_id,
            workflow=WORKFLOW_NAME,
            extra_metadata={"session_id": session_id, "workflow_stage": "submit"},
        )
        result = await build_intake_workflow(engine).ainvoke(
            {"entry": "submit", "session_id": session_id}, config=config
        )
        field_id = result.get("field_id")
        canonical_path = result.get("canonical_path")
        validation_success = bool(result.get("validation_success"))
        next_field_id = result.get("current_field_id")
        next_question = engine.question_payload_for(next_field_id) if next_field_id else None
        return SubmitAnswerResult(
            session_id=session_id,
            field_id=field_id,
            canonical_path=canonical_path,
            validation_success=validation_success,
            error_message=result.get("last_error"),
            retry_eligible=not validation_success,
            workflow_status=result.get("workflow_status", "unknown"),
            next_question=next_question,
        )
    finally:
        clear_pending_answer()
        clear_log_context()


@router.post(
    "/intake/sessions/{session_id}/request-fields",
    response_model=list[FieldRequestOutcome],
    summary="Request fields externally (browser/voice agent entry point)",
)
async def request_fields(session_id: str, payload: RequestFieldsRequest) -> list[FieldRequestOutcome]:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="request_fields")
    try:
        engine = get_intake_engine()
        try:
            return engine.request_fields(session_id, payload.requested_paths, payload.source_context)
        except SessionNotFoundError:
            raise _404("intake session not found")
    finally:
        clear_log_context()


@router.get(
    "/intake/sessions/{session_id}/profile-summary",
    response_model=ProfileSummary,
    summary="Safe profile summary (presence + counts, no values)",
)
async def profile_summary(session_id: str) -> ProfileSummary:
    try:
        return get_intake_engine().get_safe_profile_summary(session_id)
    except SessionNotFoundError:
        raise _404("intake session not found")


@router.post(
    "/intake/sessions/{session_id}/route-disclosure",
    response_model=RouteDataDisclosure,
    summary="Generate a route data-sharing preview (paths, not values)",
)
async def route_disclosure(
    session_id: str,
    payload: RouteDisclosureRequest,
    mode: str = Query(default="live", description="execution mode: live (real registry) or mock (demo overlay)"),
) -> RouteDataDisclosure:
    try:
        return _intake_engine_for_mode(mode).create_route_disclosure(
            session_id, payload.registry_id, payload.paths
        )
    except SessionNotFoundError:
        raise _404("intake session not found")
    except RouteNotFoundError:
        raise _404("registry route not found")


@router.post(
    "/intake/sessions/{session_id}/consent/route",
    response_model=RouteConsentDecision,
    summary="Grant or deny route-specific disclosure consent",
)
async def route_consent(
    session_id: str,
    payload: RouteConsentRequest,
    mode: str = Query(default="live", description="execution mode: live (real registry) or mock (demo overlay)"),
) -> RouteConsentDecision:
    try:
        return _intake_engine_for_mode(mode).grant_route_consent(
            session_id, payload.registry_id, payload.paths, payload.granted
        )
    except SessionNotFoundError:
        raise _404("intake session not found")
    except RouteNotFoundError:
        raise _404("registry route not found")


@router.post(
    "/intake/sessions/{session_id}/consent",
    response_model=ConsentReceipt,
    summary="Record collection or household-driver consent",
)
async def record_consent(session_id: str, payload: ConsentRequest) -> ConsentReceipt:
    engine = get_intake_engine()
    try:
        engine.get_session(session_id)
    except SessionNotFoundError:
        raise _404("intake session not found")
    if payload.scope is ConsentScope.COLLECTION:
        return engine.record_collection_consent(session_id)
    if payload.scope is ConsentScope.HOUSEHOLD_DRIVER:
        if not payload.driver_label:
            raise HTTPException(status_code=422, detail="driver_label required for household_driver consent")
        return engine.record_household_driver_consent(session_id, payload.driver_label)
    raise HTTPException(status_code=422, detail="unsupported consent scope")


@router.get(
    "/intake/catalog",
    response_model=list[CatalogField],
    summary="Data-driven intake field catalog for a product (safe metadata only)",
)
async def intake_catalog(
    product: InsuranceType = Query(default=InsuranceType.AUTO, description="Product type"),
) -> list[CatalogField]:
    """Return the safe, data-driven catalog so the UI is never hardcoded.

    Contains question metadata / canonical paths only - never applicant values.
    """
    catalog = IntakeFieldCatalog()
    fields: list[CatalogField] = []
    for field in catalog.enabled(product):
        fields.append(
            CatalogField(
                field_id=field.field_id,
                canonical_path=catalog.resolve_template(field.canonical_path_template),
                question=field.question,
                short_label=field.short_label,
                input_type=field.input_type.value,
                collection_group=field.collection_group.value,
                intake_phase=field.intake_phase.value,
                sensitivity=field.sensitivity.value,
                choices=list(field.choices),
                priority=field.priority,
                seed_required=field.seed_required,
                item_unit=field.item_unit,
                item_unit_required=field.item_unit_required,
                household_attestation_required=field.household_attestation_required,
                help_text=field.help_text,
            )
        )
    return fields


@router.get("/intake/checkpoints/{kind}", summary="Get a human checkpoint control definition")
async def checkpoint(kind: HumanCheckpointKind) -> Any:
    requirement = get_intake_engine().evaluate_checkpoint(kind)
    if requirement is None:
        raise _404("unknown checkpoint kind")
    return requirement


@router.delete("/intake/sessions/{session_id}", summary="Delete a session and its vault profile")
async def delete_session(session_id: str) -> dict[str, str]:
    request_id = uuid.uuid4().hex
    set_log_context(request_id=request_id, workflow=WORKFLOW_NAME, workflow_stage="delete_session")
    try:
        try:
            get_intake_engine().delete_session(session_id)
        except SessionNotFoundError:
            raise _404("intake session not found")
        return {"status": "deleted"}
    finally:
        clear_log_context()
