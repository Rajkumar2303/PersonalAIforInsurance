"""Deterministic browser executor (Issue #7, hardened in Prompt 2).

One generic, data-driven step: inspect page -> detect barriers/quote/callback/-
validation -> map fields -> fill known values (just-in-time from the vault) ->
pause for missing/unknown/consent/ambiguity/unsupported value -> checkpoint
gate -> navigate -> observe.

Prompt-2 hardenings:
- route consent is RE-CHECKED every step (never assumed permanent),
- a newly revealed canonical field outside the route disclosure scope pauses
  for expanded consent BEFORE any value is filled,
- household-driver fields pause before retrieval/fill without that consent,
- unknown REQUIRED fields pause; unknown OPTIONAL fields are left blank,
- a canonical value with no compatible destination option -> value_not_supported
  (never pick an arbitrary closest option),
- website validation/error state -> paused_validation_error (never loops),
- ambiguous field/action mappings -> paused_ambiguous (never fill/click both),
- host is re-checked after navigation (unexpected external redirect -> stop),
- fill/navigation failures become safe technical observations carrying the
  canonical PATH (never the value),
- bounded local action timeouts; no Issue #8 retry policy.

NO applicant values in any state/logs; NO Issue #8 terminal statuses/failover.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from ..core.logging import get_log_context
from ..models.browser.action import (
    BrowserActionEvent,
    CLICK,
    EXTRACT,
    FILL,
    NAVIGATE,
    PAUSE,
    SELECT,
    STATUS_BLOCKED,
    STATUS_FAILURE,
    STATUS_PAUSED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)
from ..models.browser.config import BrowserRouteConfig, FillStrategy, TransformKind
from ..models.browser.observation import (
    BrowserCheckpointObservation,
    BrowserFieldObservation,
    BrowserObservation,
    BrowserObservationType,
    BrowserPageObservation,
    BrowserQuoteObservation,
)
from ..models.browser.session import (
    BrowserActionSafety,
    BrowserSession,
    BrowserSessionStatus,
    BrowserStepResult,
)
from ..services.intake.engine import SessionNotFoundError
from .actions import ActionClassifier, Clickable
from .adapters import BrowserSiteAdapter, GenericQuoteSiteAdapter
from .detect import PageDetector
from .fill import FieldFiller, OptionNotSupportedError
from .inspect import PageInspector
from .matchers import FieldMapper
from .value_provider import BrowserValueSource

logger = logging.getLogger(__name__)

_DEFAULT_GOTO_TIMEOUT_MS = 15000


class FieldFillFailure(Exception):
    """A field fill failed. Carries ONLY the canonical path (never the value)."""

    def __init__(self, path: str, *, unsupported: bool = False) -> None:
        super().__init__("field fill failed")
        self.path = path
        self.unsupported = unsupported


@dataclass
class FillOutcome:
    """Safe aggregate of one fill loop."""

    filled: int = 0
    # Canonical paths that were successfully filled THIS loop (never values).
    # Used by post-fill human checkpoints (e.g. a licence field was just
    # filled, so the submitting click must wait for participant approval).
    filled_paths: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    consent_paths: list[str] = field(default_factory=list)
    unknown_required_ids: list[str] = field(default_factory=list)
    unknown_required_observations: list[BrowserFieldObservation] = field(default_factory=list)
    ambiguous_fields: list[str] = field(default_factory=list)
    unsupported_paths: list[str] = field(default_factory=list)
    observed_ids: list[str] = field(default_factory=list)


class BrowserExecutor:
    """Deterministic browser step engine (observation-first)."""

    def __init__(
        self,
        value_source: BrowserValueSource,
        adapter: Optional[BrowserSiteAdapter] = None,
        inspector: Optional[PageInspector] = None,
        mapper: Optional[FieldMapper] = None,
        filler: Optional[FieldFiller] = None,
        classifier: Optional[ActionClassifier] = None,
        detector: Optional[PageDetector] = None,
        goto_timeout_ms: int = _DEFAULT_GOTO_TIMEOUT_MS,
    ) -> None:
        self._values = value_source
        self._adapter = adapter or GenericQuoteSiteAdapter()
        self._inspector = inspector or PageInspector()
        self._mapper = mapper or FieldMapper()
        self._filler = filler or FieldFiller()
        self._classifier = classifier or ActionClassifier()
        self._detector = detector or PageDetector()
        self._goto_timeout_ms = goto_timeout_ms
        # Privacy-safe action events emitted during the current step.
        self._step_action_events: list[BrowserActionEvent] = []

    # --- public -----------------------------------------------------

    def merged_config(self, config: BrowserRouteConfig) -> BrowserRouteConfig:
        """Route config merged with the adapter's safe generic defaults."""
        return self._adapter.merged_config(config)

    async def start(self, page: Any, session: BrowserSession, config: BrowserRouteConfig, start_url: str) -> BrowserStepResult:
        """Navigate to the start URL (host-checked) and run the first step."""
        self._step_action_events = []
        if not await self._host_allowed(start_url, session, config):
            self._record_action(session, NAVIGATE, status=STATUS_BLOCKED)
            return self._route_changed(session, "navigation attempted to a host outside allowed_hosts")
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=self._goto_timeout_ms)
        except Exception as exc:
            self._record_action(session, NAVIGATE, status=STATUS_FAILURE)
            return self._technical_error(session, f"page load failed ({type(exc).__name__})")
        if not await self._host_allowed(page.url, session, config):
            self._record_action(session, NAVIGATE, status=STATUS_BLOCKED)
            return self._route_changed(session, "page redirected to a host outside allowed_hosts")
        self._record_action(session, NAVIGATE, status=STATUS_SUCCESS)
        session.current_url = self._sanitize_url(page.url)
        return await self._advance(page, session, config)

    async def advance(self, page: Any, session: BrowserSession, config: BrowserRouteConfig) -> BrowserStepResult:
        """Advance one step (public entry): resets per-step action events."""
        self._step_action_events = []
        return await self._advance(page, session, config)

    async def _advance(self, page: Any, session: BrowserSession, config: BrowserRouteConfig) -> BrowserStepResult:
        """One full step: inspect -> detect -> map -> fill -> checkpoint -> navigate."""
        session.current_step += 1
        session.status = BrowserSessionStatus.RUNNING
        try:
            return await self._advance_inner(page, session, config)
        except OptionNotSupportedError:
            # Unsafe to fill; no compatible destination option. Never guess.
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.VALUE_NOT_SUPPORTED,
                BrowserSessionStatus.PAUSED_VALUE_NOT_SUPPORTED,
                page_signature=session.page_signature,
                message="a canonical value has no compatible destination option; pause",
            )
        except FieldFillFailure as exc:
            if exc.unsupported:
                self._record_action(session, PAUSE, status=STATUS_PAUSED)
                return self._build_result(
                    session,
                    BrowserObservationType.VALUE_NOT_SUPPORTED,
                    BrowserSessionStatus.PAUSED_VALUE_NOT_SUPPORTED,
                    page_signature=session.page_signature,
                    unsupported_value_paths=[exc.path],
                    message=f"value not supported for canonical field {exc.path}",
                )
            self._record_action(session, PAUSE, status=STATUS_FAILURE)
            return self._technical_error(session, f"field fill failed for {exc.path}", error_paths=[exc.path])
        except Exception as exc:
            # Browser crash / context closed / navigation aborted / unexpected.
            self._record_action(session, PAUSE, status=STATUS_FAILURE)
            return self._technical_error(session, f"browser step failed ({type(exc).__name__})")

    def _record_action(
        self,
        session: BrowserSession,
        action: str,
        *,
        canonical_field: Optional[str] = None,
        status: str = STATUS_SUCCESS,
    ) -> None:
        """Record + structured-log ONE privacy-safe browser action.

        The event carries ONLY the canonical PATH, the action category, a
        status, and safe correlation ids - never a value, selector, page text,
        URL query, cookie, token, or raw Playwright data. The redacting logging
        filter strips any sensitive value defensively.
        """
        ctx = get_log_context()
        event = BrowserActionEvent(
            provider=session.registry_id or "",
            action=action,
            canonical_field=canonical_field,
            status=status,
            request_id=ctx.get("request_id"),
            trace_id=ctx.get("trace_id"),
            attempt_id=session.attempt_id,
            plan_id=session.plan_id,
            browser_session_id=session.browser_session_id,
        )
        self._step_action_events.append(event)
        logger.info(
            "browser_action provider=%s action=%s canonical_field=%s status=%s "
            "request_id=%s trace_id=%s attempt_id=%s",
            event.provider, event.action, event.canonical_field or "-", event.status,
            event.request_id or "-", event.trace_id or "-", event.attempt_id or "-",
        )

    async def _advance_inner(self, page: Any, session: BrowserSession, config: BrowserRouteConfig) -> BrowserStepResult:
        # [17] host re-check after any navigation.
        if not await self._host_allowed(page.url, session, config):
            self._record_action(session, PAUSE, status=STATUS_BLOCKED)
            return self._route_changed(session, "page left allowed_hosts")
        # [13] consent re-check every step - never assume it is permanent.
        if not self._values.has_route_consent(session.intake_session_id, session.registry_id or ""):
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.NEEDS_CONSENT,
                BrowserSessionStatus.PAUSED_NEEDS_CONSENT,
                page_signature=session.page_signature,
                message="route-disclosure consent is no longer active; not filling data",
            )
        # [9] website validation/error state (e.g. rejected value) -> pause, no loop.
        if await self._detector.validation_error_detected(page, config):
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.VALIDATION_ERROR,
                BrowserSessionStatus.PAUSED_VALIDATION_ERROR,
                page_signature=session.page_signature,
                message="website validation/error state detected; not re-submitting",
            )
        if await self._detector.access_control_detected(page, config):
            self._record_action(session, PAUSE, status=STATUS_BLOCKED)
            return self._build_result(
                session,
                BrowserObservationType.ACCESS_CONTROL_DETECTED,
                BrowserSessionStatus.STOPPED_ACCESS_CONTROL,
                message="access control / CAPTCHA barrier detected; automation stopped",
            )

        page_obs = await self._inspector.inspect(page, session.current_page_index)
        page_obs.bot_protection_present = await self._detector.bot_protection_detected(page, config)
        signature = await self._detector.page_signature(page, page_obs, config)
        if signature is not None:
            session.page_signature = signature.signature_id

        # Terminal-ish observations take precedence over filling.
        quote = await self._detector.quote_detected(page, config)
        if quote is not None:
            self._record_action(session, EXTRACT, status=STATUS_SUCCESS)
            return self._build_result(
                session,
                BrowserObservationType.QUOTE_DETECTED,
                BrowserSessionStatus.SUCCEEDED,
                page_signature=session.page_signature,
                quote=quote,
                message="quote observation captured",
            )
        if await self._detector.callback_detected(page, config):
            # No premium was returned - the blocker is preserved redacted.
            self._record_action(session, EXTRACT, status=STATUS_BLOCKED)
            return self._build_result(
                session,
                BrowserObservationType.CALLBACK_DETECTED,
                BrowserSessionStatus.SUCCEEDED,
                page_signature=session.page_signature,
                message=(
                    f"callback/phone handoff detected for {session.registry_id} "
                    f"at step {session.current_step}; no call placed (Issue #9)"
                ),
            )

        # Safety gate on clickable actions BEFORE filling/submitting.
        checkpoint = await self._evaluate_actions(page, session, config)
        if checkpoint is not None:
            return checkpoint

        outcome = await self._fill_loop(page, session, config)
        session.observed_field_ids = list(dict.fromkeys([*session.observed_field_ids, *outcome.observed_ids]))

        if outcome.ambiguous_fields:
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.AMBIGUOUS_FIELD,
                BrowserSessionStatus.PAUSED_AMBIGUOUS,
                page_signature=session.page_signature,
                ambiguous_field_ids=outcome.ambiguous_fields,
                message="a canonical field matched multiple controls; not filling either",
            )
        if outcome.unsupported_paths:
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.VALUE_NOT_SUPPORTED,
                BrowserSessionStatus.PAUSED_VALUE_NOT_SUPPORTED,
                page_signature=session.page_signature,
                unsupported_value_paths=outcome.unsupported_paths,
                message="one or more canonical values have no compatible destination option",
            )
        if outcome.unknown_required_ids:
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.UNKNOWN_EXTERNAL_FIELD,
                BrowserSessionStatus.PAUSED_UNKNOWN_FIELD,
                page_signature=session.page_signature,
                unknown_external_fields=sorted(outcome.unknown_required_ids),
                unknown_field_observations=outcome.unknown_required_observations,
                message="unmapped required website question; pause for developer mapping",
            )
        if outcome.consent_paths:
            session.pending_field_paths = outcome.consent_paths
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.NEEDS_CONSENT,
                BrowserSessionStatus.PAUSED_NEEDS_CONSENT,
                page_signature=session.page_signature,
                needs_consent_paths=outcome.consent_paths,
                message="additional route/household consent required before these fields can be filled",
            )
        if outcome.missing_paths:
            session.pending_field_paths = outcome.missing_paths
            try:
                outcomes = self._values.request(session.intake_session_id, outcome.missing_paths)
            except SessionNotFoundError:
                return self._technical_error(session, "intake session not found")
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.NEEDS_FIELD,
                BrowserSessionStatus.PAUSED_NEEDS_FIELD,
                page_signature=session.page_signature,
                missing_field_paths=outcome.missing_paths,
                pending_field_paths=outcome.missing_paths,
                message=f"missing canonical fields requested via intake (requested={len(outcomes)})",
            )

        # [licence-submission checkpoint] POST-FILL, PRE-CLICK human-checkpoint
        # gate. The fields on this screen (e.g. the driver's licence number)
        # were already filled; the action that SUBMITS them / triggers an
        # identity or database lookup must wait for explicit participant
        # approval. Fires only for checkpoint bindings configured with
        # ``post_fill_paths`` whose matched canonical paths were just filled.
        checkpoint = await self._post_fill_checkpoint(page, session, config, outcome.filled_paths)
        if checkpoint is not None:
            return checkpoint

        # All mapped fields known + filled -> find a safe bound action to continue.
        action, ambiguous_actions = await self._find_safe_action(page, session, config)
        if ambiguous_actions:
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.AMBIGUOUS_ACTION,
                BrowserSessionStatus.PAUSED_AMBIGUOUS,
                page_signature=session.page_signature,
                ambiguous_action_labels=ambiguous_actions,
                message="multiple distinct safe actions present; not clicking arbitrarily",
            )
        if action is None:
            self._record_action(session, PAUSE, status=STATUS_PAUSED)
            return self._build_result(
                session,
                BrowserObservationType.FIELDS_FILLED,
                BrowserSessionStatus.RUNNING,
                page_signature=session.page_signature,
                filled_field_count=outcome.filled,
                message="fields filled; no safe bound action found",
            )
        self._record_action(session, CLICK, status=STATUS_SUCCESS)
        await self._adapter.click_by_label(page, action)
        # [17] re-check host after the click (may redirect unexpectedly).
        if not await self._host_allowed(page.url, session, config):
            self._record_action(session, PAUSE, status=STATUS_BLOCKED)
            return self._route_changed(session, "action redirected to a host outside allowed_hosts")
        session.current_page_index += 1
        session.current_url = self._sanitize_url(page.url)
        return self._build_result(
            session,
            BrowserObservationType.FIELDS_FILLED,
            BrowserSessionStatus.RUNNING,
            page_signature=session.page_signature,
            filled_field_count=outcome.filled,
            action_type=action,
            message="known fields filled and safe navigation performed",
        )

    # --- internals --------------------------------------------------

    async def _fill_loop(self, page: Any, session: BrowserSession, config: BrowserRouteConfig) -> FillOutcome:
        """Inspect -> map -> fill known; re-inspect for conditional fields.

        Bounded (3 passes) so fill-triggered reveals cannot loop forever.
        """
        outcome = FillOutcome()
        seen: set[str] = set()

        for _pass in range(3):
            page_obs = await self._inspector.inspect(page, session.current_page_index)
            new_fields = [f for f in page_obs.fields if f.external_field_id not in seen]
            if not new_fields and _pass > 0:
                break
            for f in new_fields:
                seen.add(f.external_field_id)
            outcome.observed_ids.extend(f.external_field_id for f in new_fields)
            interim = BrowserPageObservation(
                page_index=page_obs.page_index,
                page_signature=page_obs.page_signature,
                fields=new_fields,
                controls_count=len(new_fields),
                heading=page_obs.heading,
            )
            # [19] ambiguity: a canonical binding matching multiple controls.
            ambiguous = self._mapper.ambiguities(interim, config)
            if ambiguous:
                outcome.ambiguous_fields = list(dict.fromkeys([*outcome.ambiguous_fields, *ambiguous]))
            matched, unmatched = self._mapper.map(interim, config)
            # [11] unknown REQUIRED pauses; unknown OPTIONAL is left blank.
            for obs in unmatched:
                if obs.required:
                    outcome.unknown_required_ids.append(obs.external_field_id)
                    outcome.unknown_required_observations.append(obs)
            pass_filled, pass_missing, pass_consent, pass_unsupported, pass_paths = await self._fill_known(
                page, session, [m for m in matched if m.binding.external_field_id not in ambiguous]
            )
            outcome.filled += pass_filled
            outcome.filled_paths.extend(pass_paths)
            outcome.missing_paths.extend(pass_missing)
            outcome.consent_paths.extend(pass_consent)
            outcome.unsupported_paths.extend(pass_unsupported)
            if not pass_filled:
                break
        outcome.missing_paths = sorted(set(outcome.missing_paths))
        outcome.consent_paths = sorted(set(outcome.consent_paths))
        outcome.unsupported_paths = sorted(set(outcome.unsupported_paths))
        outcome.unknown_required_ids = sorted(set(outcome.unknown_required_ids))
        return outcome

    async def _fill_known(
        self, page: Any, session: BrowserSession, matched: list[Any]
    ) -> tuple[int, list[str], list[str], list[str], list[str]]:
        filled = 0
        filled_paths: list[str] = []
        missing: list[str] = []
        consent: list[str] = []
        unsupported: list[str] = []
        registry_id = session.registry_id or ""
        for mapped in matched:
            path = mapped.canonical_path
            # [12] consent expansion: path outside the route disclosure scope.
            if not self._values.route_disclosure_covers(session.intake_session_id, registry_id, path):
                consent.append(path)
                continue
            # [14] household-driver gate BEFORE retrieval/fill (regardless of known).
            if self._values.field_gate(session.intake_session_id, path) == "household_consent_required":
                consent.append(path)
                continue
            if not self._values.known(session.intake_session_id, path):
                missing.append(path)
                continue
            # JIT value source: a route constant (non-PII, e.g. Province=Ontario)
            # is filled directly and is NEVER retrieved/logged; otherwise the
            # value lives only in this local variable and is discarded after
            # the fill (retrieved just in time from the trusted intake accessor).
            if mapped.binding.constant_value is not None:
                value = mapped.binding.constant_value
            elif mapped.binding.transform is TransformKind.COLLECTION_LENGTH:
                value = self._values.collection_length(session.intake_session_id, path)
            else:
                value = self._values.get(session.intake_session_id, path)
            if value is None:
                missing.append(path)
                continue
            is_select = (
                mapped.binding.fill_strategy is FillStrategy.SELECT
                or mapped.binding.control_type == "select"
            )
            action = SELECT if is_select else FILL
            try:
                await self._filler.fill(page, mapped.observation, mapped.binding, value)
                filled += 1
                filled_paths.append(path)
                self._record_action(session, action, canonical_field=path, status=STATUS_SUCCESS)
            except OptionNotSupportedError:
                unsupported.append(path)
                self._record_action(session, action, canonical_field=path, status=STATUS_SKIPPED)
            except Exception as exc:
                # [41] failure messages identify the canonical PATH, never the
                # value. Issue #8 classifies the final outcome later.
                self._record_action(session, action, canonical_field=path, status=STATUS_FAILURE)
                raise FieldFillFailure(path) from exc
        return filled, sorted(set(missing)), sorted(set(consent)), sorted(set(unsupported)), filled_paths

    async def _classify_actions(self, page: Any, config: BrowserRouteConfig) -> list[Clickable]:
        clickables = await self._adapter.collect_clickables(page)
        return [self._classifier.classify(label, config) for label in clickables]

    async def _evaluate_actions(
        self, page: Any, session: BrowserSession, config: BrowserRouteConfig
    ) -> Optional[BrowserStepResult]:
        for clickable in await self._classify_actions(page, config):
            if clickable.safety is BrowserActionSafety.PROHIBITED:
                self._record_action(session, PAUSE, status=STATUS_BLOCKED)
                return self._build_result(
                    session,
                    BrowserObservationType.HUMAN_CHECKPOINT,
                    BrowserSessionStatus.STOPPED_PROHIBITED,
                    page_signature=session.page_signature,
                    checkpoint=BrowserCheckpointObservation(
                        checkpoint_type=clickable.action_type,
                        label=clickable.label,
                        requires_human=True,
                        must_not_automate=True,
                        action_label=clickable.label,
                    ),
                    message="prohibited action detected (signature/payment/purchase/binding); automation stopped",
                )
            if clickable.safety is BrowserActionSafety.HUMAN_CHECKPOINT:
                # POST-FILL checkpoints (e.g. licence submission) fire AFTER
                # filling, immediately before the submitting click - NOT here.
                if clickable.post_fill:
                    continue
                # A checkpoint the participant explicitly approved must not
                # re-pause on the same session.
                if clickable.action_type in session.checkpoint_approvals:
                    continue
                self._record_action(session, PAUSE, status=STATUS_PAUSED)
                return self._build_result(
                    session,
                    BrowserObservationType.HUMAN_CHECKPOINT,
                    BrowserSessionStatus.PAUSED_HUMAN_CHECKPOINT,
                    page_signature=session.page_signature,
                    checkpoint=BrowserCheckpointObservation(
                        checkpoint_type=clickable.action_type,
                        label=clickable.label,
                        requires_human=True,
                        must_not_automate=bool(clickable.checkpoint and clickable.checkpoint.must_not_automate),
                        action_label=clickable.label,
                    ),
                    message="human checkpoint required; automation paused",
                )
        return None

    async def _post_fill_checkpoint(
        self,
        page: Any,
        session: BrowserSession,
        config: BrowserRouteConfig,
        filled_paths: list[str],
    ) -> Optional[BrowserStepResult]:
        """POST-FILL, PRE-CLICK human-checkpoint gate.

        Fires ONLY for checkpoint bindings configured with ``post_fill_paths``
        (e.g. licence-submission / identity-lookup screens). The executor may
        fill the fields on this screen automatically (including the driver's
        licence number), but the action that SUBMITS them / triggers an
        identity or database lookup must wait for explicit participant
        approval. Deterministic and independent of the site's URL structure.
        """
        if not filled_paths:
            return None
        for clickable in await self._classify_actions(page, config):
            if clickable.safety is BrowserActionSafety.PROHIBITED:
                # A prohibited boundary revealed after filling - never submit.
                self._record_action(session, PAUSE, status=STATUS_BLOCKED)
                return self._build_result(
                    session,
                    BrowserObservationType.HUMAN_CHECKPOINT,
                    BrowserSessionStatus.STOPPED_PROHIBITED,
                    page_signature=session.page_signature,
                    checkpoint=BrowserCheckpointObservation(
                        checkpoint_type=clickable.action_type,
                        label=clickable.label,
                        requires_human=True,
                        must_not_automate=True,
                        action_label=clickable.label,
                    ),
                    message="prohibited action detected after fill; automation stopped",
                )
            if clickable.safety is BrowserActionSafety.HUMAN_CHECKPOINT and clickable.post_fill:
                if clickable.action_type in session.checkpoint_approvals:
                    continue  # explicitly approved by the participant
                if not any(
                    path_part in filled
                    for filled in filled_paths
                    for path_part in clickable.post_fill_paths
                ):
                    continue  # this screen's filled fields are not the trigger
                self._record_action(session, PAUSE, status=STATUS_PAUSED)
                return self._build_result(
                    session,
                    BrowserObservationType.HUMAN_CHECKPOINT,
                    BrowserSessionStatus.PAUSED_HUMAN_CHECKPOINT,
                    page_signature=session.page_signature,
                    checkpoint=BrowserCheckpointObservation(
                        checkpoint_type=clickable.action_type,
                        label=clickable.label,
                        requires_human=True,
                        must_not_automate=bool(clickable.checkpoint and clickable.checkpoint.must_not_automate),
                        action_label=clickable.label,
                    ),
                    message=(
                        "licence/identity-submission checkpoint: fields were filled, "
                        "but the submitting action waits for explicit participant approval"
                    ),
                )
        return None

    async def _find_safe_action(
        self, page: Any, session: BrowserSession, config: BrowserRouteConfig
    ) -> tuple[Optional[str], list[str]]:
        """Return (action_label, ambiguous_action_labels).

        [20] Multiple DISTINCT safe actions is ambiguous - never click an
        arbitrary one. Duplicate identical labels collapse to one deterministic
        action (first visible in DOM).

        A POST-FILL human-checkpoint action (e.g. licence submission) is
        treated as clickable here: the checkpoint only actually PAUSES when its
        ``post_fill_paths`` were just filled (handled by the earlier post-fill
        gate). On any other screen the same Continue/Next action is a safe
        navigation - it must not be shadowed by the checkpoint binding.
        """
        safe = [
            c for c in await self._classify_actions(page, config)
            if c.safety in (BrowserActionSafety.SAFE_NAVIGATION, BrowserActionSafety.DATA_SUBMISSION)
            or (c.safety is BrowserActionSafety.HUMAN_CHECKPOINT and c.post_fill)
        ]
        if not safe:
            return None, []
        labels = [c.label for c in safe]
        if len(set(labels)) > 1:
            return None, labels
        return labels[0], []

    async def _host_allowed(self, url: str, session: BrowserSession, config: BrowserRouteConfig) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if config.allowed_hosts:
            return any(host == allowed.lower() or host.endswith("." + allowed.lower()) for allowed in config.allowed_hosts)
        # No configured hosts: sandbox defaults to localhost only; live refuses.
        if session.execution_mode.value == "live":
            return False
        return host in ("127.0.0.1", "localhost")

    @staticmethod
    def _sanitize_url(url: str) -> Optional[str]:
        """Drop query strings (may contain applicant data) from stored URLs."""
        if not url:
            return None
        return urlsplit(url)._replace(query="", fragment="").geturl()

    def _route_changed(self, session: BrowserSession, message: str) -> BrowserStepResult:
        return self._build_result(
            session,
            BrowserObservationType.ROUTE_CHANGED,
            BrowserSessionStatus.STOPPED_UNEXPECTED_HOST,
            message=message,
        )

    def _build_result(
        self,
        session: BrowserSession,
        observation_type: BrowserObservationType,
        status: BrowserSessionStatus,
        *,
        page_signature: Optional[str] = None,
        message: Optional[str] = None,
        quote: Optional[Any] = None,
        filled_field_count: int = 0,
        missing_field_paths: Optional[list[str]] = None,
        needs_consent_paths: Optional[list[str]] = None,
        pending_field_paths: Optional[list[str]] = None,
        unknown_external_fields: Optional[list[str]] = None,
        unknown_field_observations: Optional[list[BrowserFieldObservation]] = None,
        ambiguous_field_ids: Optional[list[str]] = None,
        ambiguous_action_labels: Optional[list[str]] = None,
        unsupported_value_paths: Optional[list[str]] = None,
        checkpoint: Optional[BrowserCheckpointObservation] = None,
        action_type: Optional[str] = None,
    ) -> BrowserStepResult:
        session.last_observation_type = observation_type.value
        if status not in (BrowserSessionStatus.RUNNING, BrowserSessionStatus.CREATED):
            session.status = status
        observation = BrowserObservation(
            observation_type=observation_type,
            page_index=session.current_page_index,
            page_signature=page_signature or session.page_signature,
            url=session.current_url,
            message=message,
            filled_field_count=filled_field_count,
            missing_field_paths=missing_field_paths or [],
            needs_consent_paths=needs_consent_paths or [],
            pending_field_paths=pending_field_paths or session.pending_field_paths,
            unknown_external_fields=unknown_external_fields or [],
            unknown_field_observations=unknown_field_observations or [],
            ambiguous_field_ids=ambiguous_field_ids or [],
            ambiguous_action_labels=ambiguous_action_labels or [],
            unsupported_value_paths=unsupported_value_paths or [],
            checkpoint=checkpoint,
            quote=BrowserQuoteObservation(quote_present=quote is not None, reference_present=bool(quote and quote.reference_present), raw=quote) if quote else None,
        )
        if quote is not None:
            session.quote_present = True
            session.reference_present = bool(quote.reference_present)
        session.updated_at = dt.datetime.now(dt.timezone.utc)
        if checkpoint is not None:
            session.checkpoint_type = checkpoint.checkpoint_type
        return BrowserStepResult(
            browser_session_id=session.browser_session_id,
            step=session.current_step,
            observation_type=observation_type,
            status=session.status,
            page_signature=page_signature or session.page_signature,
            filled_field_count=filled_field_count,
            missing_field_count=len(missing_field_paths or []),
            unknown_field_count=len(unknown_external_fields or []),
            message=message,
            observation=observation,
            action_events=list(self._step_action_events),
        )

    def _technical_error(self, session: BrowserSession, message: str, error_paths: Optional[list[str]] = None) -> BrowserStepResult:
        session.status = BrowserSessionStatus.FAILED
        session.updated_at = dt.datetime.now(dt.timezone.utc)
        observation = BrowserObservation(
            observation_type=BrowserObservationType.TECHNICAL_ERROR,
            page_index=session.current_page_index,
            page_signature=session.page_signature,
            url=session.current_url,
            message=message,
            error_paths=error_paths or [],
        )
        return BrowserStepResult(
            browser_session_id=session.browser_session_id,
            step=session.current_step,
            observation_type=BrowserObservationType.TECHNICAL_ERROR,
            status=BrowserSessionStatus.FAILED,
            message=message,
            observation=observation,
            action_events=list(self._step_action_events),
        )
