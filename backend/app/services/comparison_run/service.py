"""Comparison run orchestration (Issue #13, MVP).

``ComparisonRunService`` coordinates EXISTING deterministic components - Issue
#6 RoutePlanner, Issue #7 Browser Agent, Issue #8 Recovery, Issue #10 Evidence,
Issue #11 Normalization, Issue #12 Comparison - into one pollable comparison
run. It duplicates no browser/recovery/normalization business rules.

Contract:
- Each route executes independently under a bounded concurrency semaphore.
- One route failure (CAPTCHA, exception, missing info) NEVER stops other routes.
- Quotes are recorded to evidence, then AUTO-normalized (closing the Issue #11
  Prompt 2 gap at run level) and compared via Issue #12.
- Mock mode may execute the demo aggregator alternative so a confirmed
  duplicate rate source is VISIBLE (Issue #12 then keeps the direct as the
  representative). Live mode never double-submits: alternatives are classified
  as ``duplicate_rate_source`` without executing, per Issue #4/#8.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any, Optional

from ...browser.session import BrowserExecutionMode
from ...core.config import get_settings
from ...graph.browser_workflow import build_browser_workflow
from ...models.browser.session import BrowserStartRefusal, LiveExecutionGate
from ...models.browser.observation import BrowserObservation
from ...models.comparison import ComparisonPlanResult
from ...models.comparison_run import (
    ComparisonRun,
    ComparisonRunStatus,
    RouteRunStatus,
    RouteRunSummary,
)
from ...models.evidence import EvidenceEventType
from ...models.normalization import NormalizedQuote
from ...models.recovery import RecoveryDecideRequest, SourceChannel
from ...services.comparison import QuoteComparisonService
from ...services.evidence import EvidenceService, get_evidence_service
from ...services.evidence.ingest import quote_from_browser_observation
from ...services.intake import get_intake_engine
from ...services.normalization import (
    QuoteNormalizationService,
    get_quote_normalization_service,
)
from ...services.recovery import (
    RecoveryEngine,
    browser_observation_to_execution,
    get_recovery_engine,
)
from ...services.route_planner import RoutePlanner, get_route_planner
from ...browser import get_browser_manager

logger = logging.getLogger(__name__)

_MAX_BROWSER_STEPS = 20


class InMemoryComparisonRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, ComparisonRun] = {}
        self._active_by_intake: dict[str, str] = {}  # intake -> active run id

    def save(self, run: ComparisonRun) -> None:
        self._runs[run.comparison_run_id] = run
        if run.status in (ComparisonRunStatus.PREPARED, ComparisonRunStatus.RUNNING):
            self._active_by_intake[run.intake_session_id] = run.comparison_run_id
        else:
            self._active_by_intake.pop(run.intake_session_id, None)

    def get(self, run_id: str) -> Optional[ComparisonRun]:
        return self._runs.get(run_id)

    def active_for_intake(self, intake_session_id: str) -> Optional[ComparisonRun]:
        run_id = self._active_by_intake.get(intake_session_id)
        return self._runs.get(run_id) if run_id else None


class ComparisonRunService:
    """Coordinate plan -> routes -> evidence -> normalization -> comparison."""

    def __init__(
        self,
        *,
        store: Optional[InMemoryComparisonRunStore] = None,
        planner: Optional[RoutePlanner] = None,
        manager: Any = None,
        recovery: Optional[RecoveryEngine] = None,
        intake: Any = None,
        evidence: Optional[EvidenceService] = None,
        normalization: Optional[QuoteNormalizationService] = None,
        comparison: Optional[QuoteComparisonService] = None,
        mock_runtime: Any = None,
        max_concurrency: Optional[int] = None,
        route_timeout_seconds: Optional[float] = None,
        run_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._store = store or InMemoryComparisonRunStore()
        self._planner = planner
        self._manager = manager
        self._recovery = recovery
        self._intake = intake
        self._evidence = evidence
        self._normalization = normalization
        self._comparison = comparison or QuoteComparisonService()
        self._mock_runtime = mock_runtime
        self._max_concurrency = max_concurrency or get_settings().comparison_max_concurrency
        # Issue #14 safety timeouts (configurable; test overrides with small
        # values). A stuck route must never leave the whole run hanging.
        self._route_timeout_seconds = (
            route_timeout_seconds
            if route_timeout_seconds is not None
            else get_settings().comparison_route_timeout_seconds
        )
        self._run_timeout_seconds = (
            run_timeout_seconds
            if run_timeout_seconds is not None
            else get_settings().comparison_run_timeout_seconds
        )
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_run(
        self,
        intake_session_id: str,
        execution_mode: str = "mock",
        live_gate: Optional[LiveExecutionGate] = None,
    ) -> ComparisonRun:
        """Create (or reuse an active) run and start it in the background.

        ``live_gate`` is the applicant's EXPLICIT attestation required before
        any LIVE browser start (never auto-granted). When ``None`` (or not
        satisfied) a live route is still refused with ``LIVE_GATE_REQUIRED`` by
        the browser session gate - this is a safety default, not a bypass.
        """
        existing = self._store.active_for_intake(intake_session_id)
        if existing is not None:
            return existing  # idempotency: no duplicate submissions on re-click
        mode = "live" if execution_mode == "live" else "mock"
        now = dt.datetime.now(dt.timezone.utc)
        run = ComparisonRun(
            comparison_run_id=uuid.uuid4().hex,
            intake_session_id=intake_session_id,
            execution_mode=mode,
            status=ComparisonRunStatus.PREPARED,
            created_at=now,
        )
        self._store.save(run)
        self._tasks[run.comparison_run_id] = asyncio.create_task(self._run(run.comparison_run_id, live_gate))
        return run

    def get_run(self, intake_session_id: str, run_id: str) -> Optional[ComparisonRun]:
        run = self._store.get(run_id)
        if run is None or run.intake_session_id != intake_session_id:
            return None
        return run

    # ------------------------------------------------------------------
    # Mode-scoped services
    # ------------------------------------------------------------------

    @property
    def _mock(self) -> Any:
        if self._mock_runtime is None:
            from ...demo.runtime import get_demo_runtime

            self._mock_runtime = get_demo_runtime()
        return self._mock_runtime

    def _services(self, mode: str):
        if mode == "mock":
            runtime = self._mock
            runtime.start_mock_site()
            return (
                self._planner or runtime.planner,
                self._manager or runtime.manager,
                self._recovery or runtime.recovery,
                "sandbox",
            )
        return (
            self._planner or get_route_planner(),
            self._manager or get_browser_manager(),
            self._recovery or get_recovery_engine(),
            "live",
        )

    def _evidence_svc(self) -> EvidenceService:
        return self._evidence or get_evidence_service()

    def _normalization_svc(self) -> QuoteNormalizationService:
        return self._normalization or get_quote_normalization_service()

    # ------------------------------------------------------------------
    # Background runner
    # ------------------------------------------------------------------

    async def _run(self, run_id: str, live_gate: Optional[LiveExecutionGate]) -> None:
        run = self._store.get(run_id)
        if run is None:
            return
        self._update(run, status=ComparisonRunStatus.RUNNING,
                     started_at=dt.datetime.now(dt.timezone.utc))
        try:
            await self._run_routes(run, live_gate)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("comparison run failed",
                            extra={"workflow": "comparison_run", "status": "error"})
            self._update(run, status=ComparisonRunStatus.FAILED,
                         error="comparison run failed; see logs",
                         completed_at=dt.datetime.now(dt.timezone.utc))
        finally:
            self._tasks.pop(run_id, None)

    async def _run_routes(
        self, run: ComparisonRun, live_gate: Optional[LiveExecutionGate]
    ) -> None:
        session_id = run.intake_session_id
        mode = run.execution_mode
        planner, manager, recovery, mode_label = self._services(mode)
        plan = planner.plan(session_id)
        plan_id = f"plan-{session_id}"
        plan_version = plan.generated_at.isoformat()
        run = self._update(run, plan_id=plan_id)

        summaries: list[RouteRunSummary] = []
        executable: list[tuple[RouteRunSummary, Any, RecoveryEngine, str, str]] = []
        for route in plan.routes:
            summary = RouteRunSummary(
                registry_id=route.registry_id,
                display_name=route.brand_or_program,
                channel="browser",
                status=RouteRunStatus.QUEUED,
                distinct_rate_source_id=route.distinct_rate_source_id,
                is_alternative=route.is_alternative,
            )
            # Sonnet is OPERATOR-MANAGED in live mode: Compare Quotes must never
            # create a Sonnet browser session or attempt. It stays VISIBLE in the
            # route/coverage ledger as "Ready for controlled live run" and is
            # started only when the operator clicks Run Sonnet Live. Mock mode
            # preserves the existing Sonnet/mock behavior (never a real route).
            if mode == "live" and route.registry_id == "sonnet":
                summary.status = RouteRunStatus.OPERATOR_MANAGED
                summary.route_outcome_semantics = "manual_handoff"
                summary.channel = "manual"
                summary.message = "Ready for controlled live run"
                summaries.append(summary)
                continue
            if route.is_alternative and mode != "mock":
                summary.status = RouteRunStatus.DUPLICATE_RATE_SOURCE
                summary.route_outcome_semantics = "duplicate_rate_source"
                summary.message = "same distinct rate source as another provider - not executed"
                summaries.append(summary)
                continue
            if not route.is_ready:
                summary.status = RouteRunStatus.NOT_READY
                summary.message = "route not ready"
                summaries.append(summary)
                continue
            if not any(channel.kind.value == "online" for channel in route.channels):
                summary.status = RouteRunStatus.NOT_READY
                summary.message = "no online/web quote channel"
                summaries.append(summary)
                continue
            intake = self._intake or (get_intake_engine() if mode == "live" else self._mock.intake)
            if intake.route_consent_state(session_id, route.registry_id) != "granted":
                summary.status = RouteRunStatus.CONSENT_REQUIRED
                summary.message = "route-disclosure consent not granted"
                summaries.append(summary)
                continue
            summaries.append(summary)
            executable.append(
                (summary, manager, recovery, plan_id, plan_version)
            )

        run = self._update(
            run, route_summaries=summaries, total_routes=len(summaries),
            running_routes=len(executable),
        )

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_one(summary, mgr, rec, p_id, p_version) -> None:
            self._update_summary(run, summary, status=RouteRunStatus.RUNNING)
            try:
                await self._execute_route(
                    run, summary, mgr, rec, session_id, mode_label, p_id, p_version, live_gate,
                )
            except Exception as exc:  # route-local isolation - never propagates
                logger.warning(
                    "route failed locally",
                    extra={
                        "workflow": "comparison_run",
                        "registry_id": summary.registry_id,
                        "workflow_stage": "route_failed",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    },
                )
                self._update_summary(
                    run, summary, status=RouteRunStatus.FAILED,
                    message=f"route failed: {type(exc).__name__}",
                )

        async def guarded(*args) -> None:
            async with semaphore:
                await run_one(*args)

        # Per-route safety timeout is enforced inside ``_execute_route`` (it
        # wraps only the browser workflow, so the session is always closed and
        # the run can never hang on a stuck page). Issue #14.
        await asyncio.gather(*(guarded(*e) for e in executable))

        # --- auto-normalize every recorded quote, then compare ---------
        await self._normalize_and_compare(run)

        # --- attach the safe, redacted evidence overview per route -----
        # Generic, read-only: never changes status/premium; for non-quote
        # outcomes (callback / blocked / handoff) it surfaces the preserved
        # evidence record's safe metadata (timestamp, id, hash, safe URL).
        await self.attach_evidence(run)

        # --- final run status ------------------------------------------
        final = self._store.get(run.comparison_run_id) or run
        statuses = [r.status for r in final.route_summaries]
        produced = {
            RouteRunStatus.COMPARABLE, RouteRunStatus.NON_COMPARABLE,
            RouteRunStatus.ESTIMATE_ONLY, RouteRunStatus.DUPLICATE_RATE_SOURCE,
        }
        resolved = produced | {
            RouteRunStatus.CAPTCHA_BLOCKED, RouteRunStatus.UNAVAILABLE,
            RouteRunStatus.CALLBACK_REQUIRED, RouteRunStatus.MANUAL_HANDOFF,
            RouteRunStatus.NEEDS_ADDITIONAL_INFORMATION, RouteRunStatus.INELIGIBLE,
            RouteRunStatus.NOT_CURRENTLY_WRITING, RouteRunStatus.NOT_READY,
            RouteRunStatus.CONSENT_REQUIRED, RouteRunStatus.UNRESOLVED,
            RouteRunStatus.AFFINITY_RESTRICTED, RouteRunStatus.SPECIALTY_ONLY,
            RouteRunStatus.FAILED, RouteRunStatus.OPERATOR_MANAGED,
        }
        done = dt.datetime.now(dt.timezone.utc)
        # Issue #14 run-level backstop: if the whole run outlived its safety
        # deadline, resolve any still queued/running routes so the run
        # terminates honestly (never leaves the UI spinning forever).
        started = final.started_at or final.created_at
        if (done - started).total_seconds() > self._run_timeout_seconds:
            for summary in final.route_summaries:
                if summary.status in (
                    RouteRunStatus.QUEUED, RouteRunStatus.RUNNING,
                    RouteRunStatus.QUOTE_PENDING_NORMALIZATION,
                ):
                    self._update_summary(
                        final, summary, status=RouteRunStatus.UNRESOLVED,
                        message="run exceeded safe timeout - route unresolved",
                    )
            final = self._store.get(run.comparison_run_id) or run
            statuses = [r.status for r in final.route_summaries]
        if not statuses:
            self._update(final, status=ComparisonRunStatus.FAILED, completed_at=done,
                         error="no routes planned")
        elif all(s in produced for s in statuses):
            self._update(final, status=ComparisonRunStatus.COMPLETED, completed_at=done)
        elif all(s in resolved for s in statuses):
            self._update(final, status=ComparisonRunStatus.COMPLETED_WITH_PARTIAL_RESULTS,
                         completed_at=done)
        else:
            self._update(final, status=ComparisonRunStatus.RUNNING)
        self._refresh_progress(final)

    async def _execute_route(
        self, run, summary, manager, recovery, session_id, mode_label, plan_id, plan_version,
        live_gate: Optional[LiveExecutionGate] = None,
    ) -> None:
        browser_session = manager.create(
            intake_session_id=session_id,
            planned_route_id=summary.registry_id,
            execution_mode=BrowserExecutionMode.SANDBOX if mode_label == "sandbox" else BrowserExecutionMode.LIVE,
            plan_id=plan_id,
            live_gate=live_gate,
        )
        if isinstance(browser_session, BrowserStartRefusal):
            self._update_summary(run, summary, status=RouteRunStatus.FAILED,
                                 message=f"browser refused: {browser_session.reason.value}")
            return
        browser_session_id = browser_session.browser_session_id
        try:
            # Issue #14: bounded browser workflow. A stuck page is resolved to
            # "temporarily unavailable" after the route timeout. The timeout
            # does NOT cancel the Playwright coroutine (cancelling a mid-flight
            # navigation can corrupt the shared browser), it closes the session
            # via the manager (the intended Playwright way to abort in-flight
            # work) and lets the workflow unwind on its own. No blind retries.
            workflow_task = asyncio.create_task(
                build_browser_workflow(manager).ainvoke(
                    {"entry": "run", "browser_session_id": browser_session_id, "max_steps": _MAX_BROWSER_STEPS}
                )
            )
            done, _pending = await asyncio.wait(
                {workflow_task}, timeout=self._route_timeout_seconds
            )
            if workflow_task not in done:
                logger.warning(
                    "route timed out",
                    extra={
                        "workflow": "comparison_run",
                        "registry_id": summary.registry_id,
                        "workflow_stage": "route_timeout",
                        "status": "unavailable",
                    },
                )
                self._update_summary(
                    run, summary, status=RouteRunStatus.UNAVAILABLE,
                    message="route exceeded safe timeout - marked temporarily unavailable",
                )
                # Abort in-flight navigation by closing the session; then give
                # the workflow a short, bounded window to unwind (no cancel).
                try:
                    await manager.close(browser_session_id)
                except Exception:  # pragma: no cover - best-effort abort
                    pass
                try:
                    await asyncio.wait_for(
                        asyncio.shield(workflow_task), timeout=3.0
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    workflow_task.add_done_callback(_consume_task)
                return
            await workflow_task
            last = manager.last_result(browser_session_id)
            if last is None or last.observation is None:
                self._update_summary(run, summary, status=RouteRunStatus.UNRESOLVED,
                                     message="browser completed without an observation")
                return
            obs: BrowserObservation = last.observation
            execution = browser_observation_to_execution(obs)
            decision = recovery.record_observation(
                RecoveryDecideRequest(
                    plan_id=plan_id,
                    planned_route_id=summary.registry_id,
                    registry_id=summary.registry_id,
                    distinct_rate_source_id=summary.distinct_rate_source_id,
                    intake_session_id=session_id,
                    source_channel=SourceChannel.BROWSER,
                    observation_type=execution.observation_type,
                    reason=execution.reason,
                    observation_sequence=1,
                    plan_version=plan_version,
                    safe_context=execution.safe_context,
                )
            )
            self._apply_decision(summary, decision)
            # Persist the decision outcome (route-local; also covers non-quote
            # outcomes like captcha/blocked/callback where no quote is stored).
            self._update_summary(run, summary)

            if obs.quote and obs.quote.quote_present:
                quote = quote_from_browser_observation(
                    session_id, obs, plan_id=plan_id, planned_route_id=summary.registry_id,
                    registry_id=summary.registry_id,
                    distinct_rate_source_id=summary.distinct_rate_source_id or "",
                    attempt_id=f"attempt-{summary.registry_id}",
                )
                if quote is not None:
                    # Mark aggregator/broker alternatives so Issue #12's
                    # representative selection prefers the DIRECT provider.
                    if summary.is_alternative:
                        quote = quote.model_copy(
                            update={"aggregator_registry_id": summary.registry_id}
                        )
                    stored = await self._evidence_svc().record_quote_observation(session_id, quote)
                    self._update_summary(
                        run, summary,
                        status=RouteRunStatus.QUOTE_PENDING_NORMALIZATION,
                        quote_observation_id=stored.quote_id,
                        evidence_status="recorded",
                        annual_premium=stored.annual_premium,
                        firm_vs_estimate=stored.firm_vs_estimate,
                    )
        finally:
            try:
                await manager.close(browser_session_id)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    # ------------------------------------------------------------------
    # Normalization + comparison pipeline
    # ------------------------------------------------------------------

    async def _normalize_and_compare(self, run: ComparisonRun) -> None:
        final = self._store.get(run.comparison_run_id) or run
        evidence = self._evidence_svc()
        normalization = self._normalization_svc()
        normalized: list[NormalizedQuote] = []
        for summary in final.route_summaries:
            if summary.quote_observation_id is None:
                continue
            try:
                nq = await normalization.normalize(
                    final.intake_session_id, summary.quote_observation_id
                )
            except Exception:  # normalization failures are route-local
                self._update_summary(final, summary, status=RouteRunStatus.NON_COMPARABLE,
                                     message="normalization failed")
                continue
            self._update_summary(
                final, summary,
                source_quote_observation_id=nq.source_quote_observation_id,
                normalized_quote_id=nq.normalized_quote_id,
                annual_premium=nq.premium.normalized_annual_amount,
            )
            normalized.append(nq)

        if not normalized:
            return
        comparison: ComparisonPlanResult = self._comparison.evaluate(
            normalized,
            intake_session_id=final.intake_session_id,
            plan_id=final.plan_id,
        )
        final = self._store.get(run.comparison_run_id) or run
        self._update(final, comparison=comparison)

        # Map route summaries to the final comparison classification.
        by_source = {r.source_quote_observation_id: r for r in comparison.results}
        for summary in final.route_summaries:
            if summary.source_quote_observation_id is None:
                continue
            cr = by_source.get(summary.source_quote_observation_id)
            if cr is None:
                continue
            self._update_summary(
                final, summary,
                status=_status_from_comparison(cr.comparison_status.value),
                route_outcome_semantics=cr.route_outcome_semantics,
                annual_premium=cr.annual_premium,
                coverage_summary=cr.coverage_summary,
                missing_coverage_keys=cr.missing_coverage_keys,
                reason_codes=[r.value for r in cr.reason_codes],
                is_representative=cr.is_representative,
            )
        self._refresh_progress(final)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def attach_evidence(self, run: ComparisonRun) -> None:
        """Populate each route summary's safe evidence overview from the store.

        Generic, deterministic, read-only: never changes a route's status or
        premium and never fabricates a timestamp/id/hash. For each route it
        picks ONE preserved evidence record (preferring the terminal-relevant
        one: callback_observed -> access-control/bot barrier -> recovery_decision
        -> most recent) and exposes only its safe metadata. Routes without
        evidence keep ``evidence_status="unavailable"`` and no evidence fields.
        """
        final = self._store.get(run.comparison_run_id) or run
        evidence = self._evidence_svc()
        sid = final.intake_session_id
        quotes = await evidence.list_quote_observations(sid)
        for summary in final.route_summaries:
            records = await evidence.list_by_route(sid, summary.registry_id)
            quote_count = sum(
                1 for q in quotes
                if q.registry_id == summary.registry_id
                or q.planned_route_id == summary.registry_id
            )
            primary = _primary_evidence(records)
            if primary is None:
                if quote_count:
                    self._update_summary(
                        final, summary, evidence_status="recorded", quote_count=quote_count
                    )
                continue
            self._update_summary(
                final, summary,
                evidence_status="recorded",
                evidence_observed_at=_iso_z(primary.observed_at),
                evidence_id=primary.evidence_id,
                evidence_content_hash=primary.content_hash,
                safe_source_url=primary.safe_url,
                terminal_reason=_terminal_reason(records, summary),
                quote_count=quote_count,
            )

    def _apply_decision(self, summary: RouteRunSummary, decision: Any) -> None:
        summary.terminal_status = decision.terminal_status.value if decision.terminal_status else None
        summary.route_outcome_semantics = decision.terminal_status.value if decision.terminal_status else None
        summary.reason_codes = list(decision.reason_codes)
        status = _status_from_terminal(decision.terminal_status.value if decision.terminal_status else None)
        summary.status = status

    def _update(self, run: ComparisonRun, **updates: Any) -> ComparisonRun:
        updated = run.model_copy(update=updates)
        self._store.save(updated)
        return updated

    def _update_summary(self, run: ComparisonRun, summary: RouteRunSummary, **updates: Any) -> None:
        merged = summary.model_copy(update=updates)
        final = self._store.get(run.comparison_run_id) or run
        summaries = [
            merged if r.registry_id == merged.registry_id else r
            for r in final.route_summaries
        ]
        self._update(final, route_summaries=summaries)
        self._refresh_progress(final)

    def _refresh_progress(self, run: ComparisonRun) -> None:
        final = self._store.get(run.comparison_run_id) or run
        done = {
            RouteRunStatus.COMPARABLE, RouteRunStatus.NON_COMPARABLE,
            RouteRunStatus.ESTIMATE_ONLY, RouteRunStatus.DUPLICATE_RATE_SOURCE,
            RouteRunStatus.CAPTCHA_BLOCKED, RouteRunStatus.UNAVAILABLE,
            RouteRunStatus.CALLBACK_REQUIRED, RouteRunStatus.MANUAL_HANDOFF,
            RouteRunStatus.NEEDS_ADDITIONAL_INFORMATION, RouteRunStatus.INELIGIBLE,
            RouteRunStatus.NOT_CURRENTLY_WRITING, RouteRunStatus.NOT_READY,
            RouteRunStatus.CONSENT_REQUIRED, RouteRunStatus.UNRESOLVED,
            RouteRunStatus.AFFINITY_RESTRICTED, RouteRunStatus.SPECIALTY_ONLY,
            RouteRunStatus.FAILED,
        }
        completed = sum(1 for r in final.route_summaries if r.status in done)
        running = sum(1 for r in final.route_summaries if r.status in (RouteRunStatus.QUEUED, RouteRunStatus.RUNNING, RouteRunStatus.QUOTE_PENDING_NORMALIZATION))
        self._update(final, completed_routes=completed, running_routes=running,
                     total_routes=len(final.route_summaries))


def _consume_task(task: asyncio.Task) -> None:
    """Retrieve/suppress an abandoned task's result (avoids GC warnings)."""
    if not task.cancelled():
        task.exception()  # noqa: B018 - consume so GC has no pending exception


# Evidence event types that best represent a terminal (non-quote) outcome, in
# priority order for the summary's primary evidence record. Never includes
# attempt lifecycle noise so the frontend shows the most meaningful record.
_EVIDENCE_PRIORITY: dict[str, int] = {
    EvidenceEventType.CALLBACK_OBSERVED.value: 0,
    EvidenceEventType.BLOCKING_ACCESS_CONTROL_OBSERVED.value: 1,
    EvidenceEventType.BOT_PROTECTION_OBSERVED.value: 2,
    EvidenceEventType.RECOVERY_DECISION.value: 3,
}


def _iso_z(value: dt.datetime) -> str:
    """Format a datetime as an exact Z-suffixed UTC ISO string (unchanged)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _event_name(record: Any) -> str:
    ev = getattr(record, "event_type", None)
    return getattr(ev, "value", None) or str(ev or "")


def _primary_evidence(records: list[Any]) -> Any:
    """Pick the single terminal-relevant evidence record, deterministically."""
    if not records:
        return None
    return min(
        records,
        key=lambda r: (
            _EVIDENCE_PRIORITY.get(_event_name(r), 99),
            getattr(r, "observed_at", None) or dt.datetime.min,
        ),
    )


def _terminal_reason(records: list[Any], summary: RouteRunSummary) -> Optional[str]:
    """A safe terminal reason: recovery decision's reason code if recorded,
    else the summary's own reason code / terminal status. Never fabricated."""
    for record in sorted(records, key=lambda r: getattr(r, "observed_at", None) or dt.datetime.min):
        if _event_name(record) == EvidenceEventType.RECOVERY_DECISION.value:
            payload = getattr(record, "payload", None)
            codes = getattr(payload, "reason_codes", None)
            if codes:
                return codes[0]
    if summary.reason_codes:
        return summary.reason_codes[0]
    return summary.terminal_status


def _status_from_terminal(terminal: Optional[str]) -> RouteRunStatus:
    mapping = {
        "estimate_only": RouteRunStatus.ESTIMATE_ONLY,
        "blocked": RouteRunStatus.CAPTCHA_BLOCKED,
        "callback_required": RouteRunStatus.CALLBACK_REQUIRED,
        "manual_handoff": RouteRunStatus.MANUAL_HANDOFF,
        "ineligible": RouteRunStatus.INELIGIBLE,
        "affinity_restricted": RouteRunStatus.AFFINITY_RESTRICTED,
        "specialty_only": RouteRunStatus.SPECIALTY_ONLY,
        "not_currently_writing": RouteRunStatus.NOT_CURRENTLY_WRITING,
        "unreachable": RouteRunStatus.UNAVAILABLE,
        "unresolved": RouteRunStatus.UNRESOLVED,
        "duplicate_rate_source": RouteRunStatus.DUPLICATE_RATE_SOURCE,
    }
    return mapping.get(terminal or "", RouteRunStatus.UNRESOLVED)


def _status_from_comparison(comparison_status: str) -> RouteRunStatus:
    mapping = {
        "comparable": RouteRunStatus.COMPARABLE,
        "insufficient_coverage_information": RouteRunStatus.NON_COMPARABLE,
        "coverage_mismatch": RouteRunStatus.NON_COMPARABLE,
        "normalization_incomplete": RouteRunStatus.NON_COMPARABLE,
        "estimate_only": RouteRunStatus.ESTIMATE_ONLY,
        "duplicate_rate_source": RouteRunStatus.DUPLICATE_RATE_SOURCE,
    }
    return mapping.get(comparison_status, RouteRunStatus.NON_COMPARABLE)


_run_service: Optional[ComparisonRunService] = None


def get_comparison_run_service() -> ComparisonRunService:
    global _run_service
    if _run_service is None:
        _run_service = ComparisonRunService()
    return _run_service


def reset_comparison_run_service() -> None:
    global _run_service
    _run_service = None
