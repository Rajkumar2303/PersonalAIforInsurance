"""Comparison orchestration (Issue #8.5 integration checkpoint).

GLUE ONLY. ``ComparisonOrchestrator`` chains the existing deterministic
services - Issue #6 RoutePlanner -> Issue #7 Browser Agent -> Issue #8
Recovery Engine - into one async, pollable job. It duplicates NO planner /
browser / recovery business rules and contains NO LLM.

- ``execution_mode=mock`` (DEFAULT): uses the isolated demo overlay
  (``app/demo/runtime.DemoRuntime``) + the local mock quote site. Synthetic
  routes never appear in real market counts / dedup / reporting / live.
- ``execution_mode=live``: uses the real singletons (real registry, real
  browser manager, real recovery engine). Existing Issue #7 live gates are
  enforced per route (verified route + live gate + consent + config), so with
  no verified route configs live simply reports not-configured / refused.

All job payloads carry SAFE metadata only - no applicant values. A raw observed
premium amount (annual) is included when a quote is observed; it is NEVER
labelled comparable (``quote_pending_normalization`` is the boundary until
Issue #11).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..browser.session import BrowserExecutionMode, BrowserSessionStatus
from ..core.config import get_settings
from ..graph.browser_workflow import build_browser_workflow
from ..models.browser.session import BrowserStartRefusal
from ..models.browser.observation import BrowserObservation
from ..models.recovery import RecoveryDecideRequest, RecoveryDecision, SourceChannel
from ..services.intake import get_intake_engine
from ..services.intake.engine import IntakeEngine
from ..services.recovery import (
    RecoveryEngine,
    browser_observation_to_execution,
    get_recovery_engine,
)
from ..services.route_planner import get_route_planner
from ..services.route_planner.planner import RoutePlanner
from ..browser import get_browser_manager
from ..browser.session import BrowserSessionManager

logger = logging.getLogger(__name__)


# --- safe job models ---------------------------------------------------

class ComparisonRoutePlanRow(BaseModel):
    """Safe pre-flight plan row (paths + public market data only)."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    brand_or_program: str
    distinct_rate_source_id: Optional[str] = None
    is_alternative: bool = False
    deduplication_status: str = ""
    is_ready: bool = False
    route_status: str = "blocked"
    blockers: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class ComparisonRouteResult(BaseModel):
    """One provider's comparison outcome (safe; premium is the raw observed value)."""

    model_config = ConfigDict(extra="forbid")

    registry_id: str
    brand_or_program: str
    distinct_rate_source_id: Optional[str] = None
    is_alternative: bool = False
    # Machine status token (frontend maps to a safe label):
    # searching | quote_received | estimate_received | blocked | callback_required |
    # manual_handoff | ineligible | affinity_restricted | specialty_only |
    # not_currently_writing | unreachable | unresolved | duplicate_rate_source |
    # consent_required | not_ready | refused | not_configured | error
    status: str = "searching"
    lifecycle_status: Optional[str] = None
    recovery_action: Optional[str] = None
    terminal_status: Optional[str] = None
    reason_codes: list[str] = Field(default_factory=list)
    quote_pending_normalization: bool = False
    quote_received: bool = False
    annual_amount_parsed: Optional[float] = None
    attempted: bool = False
    message: Optional[str] = None  # safe, non-sensitive


class ComparisonJob(BaseModel):
    """Safe, pollable comparison job state."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    intake_session_id: str
    execution_mode: str  # mock | live
    status: str = "queued"  # queued | running | done | failed
    plan: list[ComparisonRoutePlanRow] = Field(default_factory=list)
    routes: list[ComparisonRouteResult] = Field(default_factory=list)
    error: Optional[str] = None  # safe
    created_at: dt.datetime
    updated_at: dt.datetime


# --- in-memory store ----------------------------------------------------

class InMemoryComparisonStore:
    """Small in-memory job store (Issue #13 will provide a dashboard/DB)."""

    def __init__(self) -> None:
        self._jobs: dict[str, ComparisonJob] = {}

    def save(self, job: ComparisonJob) -> None:
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Optional[ComparisonJob]:
        return self._jobs.get(job_id)

    def list(self) -> list[ComparisonJob]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at)


# --- orchestrator -------------------------------------------------------

#: Safety cap on browser steps per run (never an infinite loop).
_MAX_BROWSER_STEPS = 20


class ComparisonOrchestrator:
    """Deterministic glue: plan -> (per ready route) browser -> recovery."""

    def __init__(
        self,
        store: Optional[InMemoryComparisonStore] = None,
        mock_runtime: Any = None,
    ) -> None:
        self._store = store or InMemoryComparisonStore()
        # ``mock_runtime`` is injected by tests (isolated temp overlay); the
        # production default is the data-driven demo runtime.
        self._mock_runtime = mock_runtime
        self._tasks: dict[str, asyncio.Task] = {}
        self._gates: dict[str, Any] = {}  # job_id -> live gate (safe booleans)

    # --- public API ----------------------------------------------------

    def start_compare(
        self,
        intake_session_id: str,
        mode: str,
        live_gate: Any = None,
    ) -> ComparisonJob:
        """Create a comparison job and run it in the background (poll via get_job)."""
        normalized = "live" if mode == "live" else "mock"
        now = dt.datetime.now(dt.timezone.utc)
        job = ComparisonJob(
            job_id=uuid.uuid4().hex,
            intake_session_id=intake_session_id,
            execution_mode=normalized,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        self._store.save(job)
        self._gates[job.job_id] = live_gate
        self._tasks[job.job_id] = asyncio.create_task(self._run(job.job_id))
        return job

    def get_job(self, job_id: str) -> Optional[ComparisonJob]:
        return self._store.get(job_id)

    def list_jobs(self) -> list[ComparisonJob]:
        return self._store.list()

    # --- mode-scoped service resolution --------------------------------

    @property
    def _mock(self) -> Any:
        """The demo runtime (data-driven overlay + mock site)."""
        if self._mock_runtime is None:
            from ..demo.runtime import get_demo_runtime

            self._mock_runtime = get_demo_runtime()
        return self._mock_runtime

    def _services(self, mode: str) -> tuple[RoutePlanner, BrowserSessionManager, RecoveryEngine, IntakeEngine, str]:
        """Return (planner, browser_manager, recovery, intake, mode_label)."""
        if mode == "mock":
            runtime = self._mock
            runtime.start_mock_site()  # idempotent; binds localhost only
            return runtime.planner, runtime.manager, runtime.recovery, runtime.intake, "sandbox"
        # LIVE: real singletons; existing live gates still apply per route.
        return get_route_planner(), get_browser_manager(), get_recovery_engine(), get_intake_engine(), "live"

    # --- background runner ---------------------------------------------

    async def _run(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            return
        self._update(job, status="running")
        live_gate = self._gates.pop(job_id, None)
        try:
            await self._run_routes(job, live_gate)
            self._update(job, status="done")
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "comparison job failed",
                extra={"workflow": "comparison", "workflow_stage": "run", "status": "error"},
            )
            self._update(job, status="failed", error="comparison failed; see logs")
        finally:
            self._tasks.pop(job_id, None)

    async def _run_routes(self, job: ComparisonJob, live_gate: Any = None) -> None:
        session_id = job.intake_session_id
        mode = job.execution_mode
        planner, manager, recovery, intake, mode_label = self._services(mode)
        plan = planner.plan(session_id)
        plan_id = f"plan-{session_id}"
        plan_version = plan.generated_at.isoformat()

        for route in plan.routes:
            row = ComparisonRoutePlanRow(
                registry_id=route.registry_id,
                brand_or_program=route.brand_or_program,
                distinct_rate_source_id=route.distinct_rate_source_id,
                is_alternative=route.is_alternative,
                deduplication_status=route.deduplication_status,
                is_ready=route.is_ready,
                route_status=route.route_status,
                blockers=sorted({b.kind.value for b in route.blockers}),
                channels=sorted({c.kind.value for c in route.channels}),
            )
            job.plan.append(row)
            result = ComparisonRouteResult(
                registry_id=route.registry_id,
                brand_or_program=route.brand_or_program,
                distinct_rate_source_id=route.distinct_rate_source_id,
                is_alternative=route.is_alternative,
                status="searching",
            )

            if route.is_alternative:
                # Confirmed duplicate member of a rate-source group: Issue #8
                # records it as duplicate_rate_source without executing it.
                decision = recovery.classify_unused_alternative(
                    plan_id=plan_id,
                    registry_id=route.registry_id,
                    distinct_rate_source_id=route.distinct_rate_source_id,
                )
                self._apply_decision(result, decision)
                result.status = "duplicate_rate_source"
                result.message = "same distinct rate source as another provider - not executed"
                job.routes.append(result)
                self._touch(job)
                continue

            if not route.is_ready:
                result.status = "not_ready"
                result.message = "route not ready" + (f" ({', '.join(row.blockers)})" if row.blockers else "")
                job.routes.append(result)
                self._touch(job)
                continue

            if not any(channel.kind.value == "online" for channel in route.channels):
                result.status = "not_ready"
                result.message = "no online/web quote channel"
                job.routes.append(result)
                self._touch(job)
                continue

            # Consent gate (explicit Issue #5 route-disclosure consent).
            consent_state = intake.route_consent_state(session_id, route.registry_id)
            if consent_state != "granted":
                result.status = "consent_required"
                result.message = "route-disclosure consent not granted"
                job.routes.append(result)
                self._touch(job)
                continue

            result.attempted = True
            await self._execute_route(
                job, result, manager, recovery, session_id, route.registry_id,
                route.distinct_rate_source_id, plan_id, plan_version, mode_label, live_gate,
            )
            job.routes.append(result)
            self._touch(job)

    async def _execute_route(
        self,
        job: ComparisonJob,
        result: ComparisonRouteResult,
        manager: BrowserSessionManager,
        recovery: RecoveryEngine,
        session_id: str,
        registry_id: str,
        distinct_rate_source_id: Optional[str],
        plan_id: str,
        plan_version: str,
        mode_label: str,
        live_gate: Any = None,
    ) -> None:
        browser_session = manager.create(
            intake_session_id=session_id,
            planned_route_id=registry_id,
            execution_mode=BrowserExecutionMode.SANDBOX if mode_label == "sandbox" else BrowserExecutionMode.LIVE,
            plan_id=plan_id,
            live_gate=live_gate,
        )
        if isinstance(browser_session, BrowserStartRefusal):
            result.status = "refused"
            result.message = f"browser refused: {browser_session.reason.value}"
            return

        browser_session_id = browser_session.browser_session_id
        try:
            state = await build_browser_workflow(manager).ainvoke(
                {
                    "entry": "run",
                    "browser_session_id": browser_session_id,
                    "max_steps": _MAX_BROWSER_STEPS,
                }
            )
            last = manager.last_result(browser_session_id)
            if last is None or last.observation is None:
                result.status = "unresolved"
                result.message = "browser completed without an observation"
                return
            obs: BrowserObservation = last.observation
            execution = browser_observation_to_execution(obs)
            decision = recovery.record_observation(
                RecoveryDecideRequest(
                    plan_id=plan_id,
                    planned_route_id=registry_id,
                    registry_id=registry_id,
                    distinct_rate_source_id=distinct_rate_source_id,
                    intake_session_id=session_id,
                    source_channel=SourceChannel.BROWSER,
                    observation_type=execution.observation_type,
                    reason=execution.reason,
                    observation_sequence=1,
                    plan_version=plan_version,
                    safe_context=execution.safe_context,
                )
            )
            self._apply_decision(result, decision)
            if obs.quote and obs.quote.quote_present and obs.quote.raw.annual_amount_parsed is not None:
                result.quote_received = True
                result.annual_amount_parsed = obs.quote.raw.annual_amount_parsed
                if decision.quote_pending_normalization:
                    result.status = "quote_received"
        finally:
            try:
                await manager.close(browser_session_id)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    # --- helpers -------------------------------------------------------

    def _apply_decision(self, result: ComparisonRouteResult, decision: RecoveryDecision) -> None:
        result.lifecycle_status = decision.lifecycle_status.value if decision.lifecycle_status else None
        result.recovery_action = decision.recommended_action.value if decision.recommended_action else None
        result.terminal_status = decision.terminal_status.value if decision.terminal_status else None
        result.reason_codes = list(decision.reason_codes)
        result.quote_pending_normalization = decision.quote_pending_normalization
        result.status = _status_from_decision(decision)

    def _update(self, job: ComparisonJob, **updates: Any) -> None:
        updated = job.model_copy(update={**updates, "updated_at": dt.datetime.now(dt.timezone.utc)})
        self._store.save(updated)

    def _touch(self, job: ComparisonJob) -> None:
        self._update(job, status=job.status)


def _status_from_decision(decision: RecoveryDecision) -> str:
    """Map a recovery decision to a safe display status token."""
    if decision.quote_pending_normalization:
        return "quote_received"
    status = decision.terminal_status.value if decision.terminal_status else None
    mapping = {
        "estimate_only": "estimate_received",
        "blocked": "blocked",
        "callback_required": "callback_required",
        "manual_handoff": "manual_handoff",
        "ineligible": "ineligible",
        "affinity_restricted": "affinity_restricted",
        "specialty_only": "specialty_only",
        "not_currently_writing": "not_currently_writing",
        "unreachable": "unreachable",
        "unresolved": "unresolved",
        "duplicate_rate_source": "duplicate_rate_source",
    }
    if status in mapping:
        return mapping[status]
    return "unresolved"


_orchestrator: Optional[ComparisonOrchestrator] = None


def get_comparison_orchestrator() -> ComparisonOrchestrator:
    """Cached default comparison orchestrator (data-driven demo runtime)."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ComparisonOrchestrator()
    return _orchestrator


def reset_comparison_orchestrator() -> None:
    """Drop the cached singleton (test isolation)."""
    global _orchestrator
    _orchestrator = None
