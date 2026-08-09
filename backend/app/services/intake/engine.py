"""Deterministic intake engine (Issue #5).

No LLM is required for any decision here: which canonical field is missing,
whether a field already exists, how it is validated, whether consent exists,
whether a value is sensitive, whether a route may receive a field, and which
profile path to update are all resolved via the Pydantic schema + field catalog
+ canonical paths + redaction + consent receipts.

Privacy: raw values are validated and stored ONLY through the profile vault
keyed by ``profile_id``. They never enter session metadata, traced graph state,
logs, or consent receipts.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, Optional

from pydantic import ValidationError

from ...core.redaction import ONTARIO_LICENCE_PATTERN
from ...models.insurance import (
    AddressInformation,
    ApplicantIdentity,
    ApplicantInformation,
    AutoInsuranceProfile,
    ConsentState,
    ContactInformation,
    InsuranceProfile,
    InsuranceType,
    Province,
    QuoteMode,
)
from ...models.insurance.auto.driver import DriverInformation, LicenceIdentity
from ...models.insurance.auto.vehicle import VehicleIdentity, VehicleInformation
from ...models.insurance.enums import LicenceClass, LicenceStatus
from ...models.insurance.paths import FieldPathError, is_missing, resolve
from ...models.insurance.profile import ProfileUpdateError
from ...models.intake.checkpoints import HumanCheckpointKind
from ...models.intake.consent import ConsentReceipt, ConsentScope
from ...models.intake.field_catalog import FieldSensitivity, InputType, IntakeFieldDefinition, IntakePhase
from ...models.intake.route import RouteConsentDecision, RouteDataDisclosure, RouteDataDisclosureItem
from ...models.intake.session import (
    FieldRequestOutcome,
    FieldRequestState,
    IntakeSession,
    IntakeSessionStatus,
    ProductGateResult,
    ProfileSummary,
    ProfileSummaryField,
    SafeQuestion,
    SubmitAnswerResult,
)
from ..market_registry import MarketRegistryService
from .catalog import IntakeFieldCatalog
from .checkpoints import CheckpointService
from .consent import ConsentService
from .session_store import InMemorySessionStore
from .vault import InMemoryProfileVault, ProfileVault

logger = logging.getLogger(__name__)


class SessionNotFoundError(KeyError):
    """Raised when an unknown session_id is used."""


class RouteNotFoundError(KeyError):
    """Raised when a route/registry_id is unknown for disclosure/consent."""


def _input_type_error(field: IntakeFieldDefinition, value: Any) -> Optional[str]:
    """Deterministic sanity check from the catalog's ``input_type``.

    The canonical Pydantic schema remains the authoritative validator for
    scalar fields (via ``profile.updated``); this is an early deterministic
    guard plus the gate for list-item unit fields.
    """
    kind = field.input_type
    if kind in (InputType.INTEGER, InputType.YEARS):
        if isinstance(value, bool) or not isinstance(value, int):
            return "expected a whole number"
    elif kind in (InputType.FLOAT, InputType.CURRENCY):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "expected a number"
    elif kind is InputType.BOOLEAN:
        if not isinstance(value, bool):
            return "expected yes or no"
    elif kind in (InputType.TEXT, InputType.LONG_TEXT, InputType.POSTAL_CODE, InputType.PHONE, InputType.EMAIL):
        if not isinstance(value, str) or not value.strip():
            return "expected text"
    elif kind is InputType.VIN:
        if not isinstance(value, str) or len(value.strip()) != 17:
            return "VIN must be exactly 17 characters"
    elif kind is InputType.LICENCE:
        if not isinstance(value, str) or not ONTARIO_LICENCE_PATTERN.fullmatch(value.strip()):
            return "invalid licence number format"
    elif kind is InputType.DATE:
        if isinstance(value, str):
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                return "invalid date"
        elif not isinstance(value, dt.date):
            return "invalid date"
    elif kind is InputType.SINGLE_SELECT:
        if value not in field.choices:
            return "choose a valid option"
    elif kind is InputType.MULTI_SELECT:
        if not isinstance(value, list) or not set(value) <= set(field.choices):
            return "choose valid options"
    return None


def _build_driver(pending: dict[str, Any]) -> dict[str, Any]:
    """Assemble a typed DriverInformation from collected unit fields."""
    licence = LicenceIdentity(
        name_on_licence=pending["driver_name_on_licence"],
        licence_number=pending["driver_licence_number"],
        province=Province.ON,
        licence_class=LicenceClass.G,
        status=LicenceStatus.VALID,
        expiry_date=pending["driver_licence_expiry"],
    )
    return DriverInformation(licence=licence).model_dump(mode="python")


def _build_vehicle(pending: dict[str, Any]) -> dict[str, Any]:
    """Assemble a typed VehicleInformation from collected unit fields."""
    identity = VehicleIdentity(
        vin=pending["vehicle_vin"],
        model_year=pending["vehicle_year"],
        make=pending["vehicle_make"],
        model=pending["vehicle_model"],
    )
    return VehicleInformation(identity=identity).model_dump(mode="python")


# Schema-composition boundary for typed list items (2 units today; extensible).
# Not per-question and not insurer-specific - this is the small typed-object
# assembly registry.
_UNIT_BUILDERS: dict[str, Any] = {
    "driver": _build_driver,
    "vehicle": _build_vehicle,
}


class IntakeEngine:
    """Deterministic, catalog-driven progressive intake core."""

    def __init__(
        self,
        catalog: Optional[IntakeFieldCatalog] = None,
        vault: Optional[ProfileVault] = None,
        sessions: Optional[InMemorySessionStore] = None,
        consent: Optional[ConsentService] = None,
        registry: Optional[MarketRegistryService] = None,
    ) -> None:
        self._catalog = catalog or IntakeFieldCatalog()
        self._vault: ProfileVault = vault or InMemoryProfileVault()
        self._sessions = sessions or InMemorySessionStore()
        self._consent = consent or ConsentService()
        self._registry = registry or MarketRegistryService()
        self._checkpoints = CheckpointService()
        # Raw pending unit/seed values - in-memory only, never serialized,
        # never traced/logged; cleared on completion/deletion.
        self._pending_units: dict[str, dict[str, dict[str, Any]]] = {}
        self._seed_values: dict[str, dict[str, Any]] = {}

    # --- session lifecycle --------------------------------------------

    def create_session(self, insurance_type: InsuranceType) -> tuple[IntakeSession, ProductGateResult]:
        supported = insurance_type is InsuranceType.AUTO
        gate = ProductGateResult(
            insurance_type=insurance_type,
            is_supported=supported,
            status="started" if supported else "product_not_implemented",
        )
        now = dt.datetime.now(dt.timezone.utc)
        session = IntakeSession(
            session_id=uuid.uuid4().hex,
            profile_id=None,
            insurance_type=insurance_type,
            status=IntakeSessionStatus.ACTIVE if supported else IntakeSessionStatus.PRODUCT_REJECTED,
            created_at=now,
            updated_at=now,
        )
        self._sessions.save(session)
        if supported:
            # Explicit collection consent recorded at journey start (the
            # applicant requested this quote journey). Route-disclosure consent
            # remains a separate, per-route explicit step.
            self._consent.record(
                session,
                ConsentScope.COLLECTION,
                granted=True,
                purpose="permission to collect and store applicant information for this journey",
            )
        logger.info(
            "intake session created",
            extra={
                "workflow": "intake_engine",
                "workflow_stage": "create_session",
                "status": "ok",
                "insurance_type": insurance_type.value,
                "is_supported": supported,
            },
        )
        return session, gate

    def get_session(self, session_id: str) -> IntakeSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    def delete_session(self, session_id: str) -> None:
        session = self.get_session(session_id)
        if session.profile_id:
            self._vault.delete(session.profile_id)
        self._consent.delete_session(session_id)
        self._sessions.delete(session_id)
        self._pending_units.pop(session_id, None)
        self._seed_values.pop(session_id, None)

    # --- gates --------------------------------------------------------

    def check_supported(self, session: IntakeSession) -> bool:
        return session.insurance_type is InsuranceType.AUTO

    def has_collection_consent(self, session: IntakeSession) -> bool:
        """Collection consent: profile ConsentState timestamp OR a receipt."""
        if self._consent.has_active(session.session_id, ConsentScope.COLLECTION):
            return True
        if session.profile_id is None:
            return False
        profile = self._vault.get(session.profile_id)
        return profile is not None and profile.consent.consent_timestamp is not None

    def compute_missing_counts(self, session: IntakeSession) -> tuple[int, int]:
        """(missing_field_count, completed_field_count) over catalog fields."""
        fields = self._catalog.enabled(InsuranceType.AUTO)
        if session.profile_id is None:
            seed = self._seed_values.get(session.session_id, {})
            done = sum(1 for f in fields if f.field_id in seed)
            return len(fields) - done, done
        profile = self._vault.get(session.profile_id)
        if profile is None:
            return len(fields), 0
        done = sum(1 for f in fields if not self._field_missing(profile, f))
        return len(fields) - done, done

    # --- next question ------------------------------------------------

    def get_next_question(self, session_id: str) -> tuple[IntakeSession, Optional[SafeQuestion]]:
        session = self.get_session(session_id)
        question = self._next_question(session)
        return session, question

    def select_next(self, session: IntakeSession) -> tuple[Optional[str], Optional[str]]:
        """Return (field_id, concrete_path) for the next question (or None)."""
        question = self._next_question(session)
        if question is None:
            return None, None
        return question.field_id, question.canonical_path

    def question_payload_for(self, field_id: str) -> Optional[SafeQuestion]:
        field = self._catalog.get(field_id)
        if field is None:
            return None
        return self._question_payload(field)

    def _next_question(self, session: IntakeSession) -> Optional[SafeQuestion]:
        field = self._pick_next_field(session)
        if field is None:
            status = self._terminal_status(session)
            session = self._touch(session, status=status, current_field_id=None, current_canonical_path=None)
            return None
        concrete = self._catalog.resolve_template(field.canonical_path_template)
        session = self._touch(session, current_field_id=field.field_id, current_canonical_path=concrete)
        return self._question_payload(field)

    def _pick_next_field(self, session: IntakeSession) -> Optional[IntakeFieldDefinition]:
        product = InsuranceType.AUTO
        if session.profile_id is None:
            if session.current_field_id:
                field = self._catalog.get(session.current_field_id)
                if field and self._seed_missing(session, field):
                    return field
            for field in self._catalog.seed_fields(product):
                if self._seed_missing(session, field):
                    return field
            return None
        profile = self._vault.get(session.profile_id)
        if profile is None:
            return None
        if session.current_field_id:
            field = self._catalog.get(session.current_field_id)
            if field and self._field_needs_asking(session, profile, field):
                return field
        candidates: list[IntakeFieldDefinition] = []
        seen: set[str] = set()

        def add(field: Optional[IntakeFieldDefinition]) -> None:
            if field is not None and field.field_id not in seen:
                candidates.append(field)
                seen.add(field.field_id)

        # unit blockers: materialize containers referenced by starter/requested
        for field in self._catalog.enabled(product):
            if (
                field.item_unit
                and field.item_unit_required
                and field.enabled
                and not self._container_exists(profile, field)
            ):
                for required in self._catalog.unit_fields(product, field.item_unit):
                    if required.enabled and self._field_needs_asking(session, profile, required):
                        add(required)
        # requested (just-in-time) fields
        for field_id in session.requested_fields:
            field = self._catalog.get(field_id)
            if (
                field is not None
                and field.enabled
                and not field.household_attestation_required
                and field.field_id not in session.declined_fields
                and self._field_needs_asking(session, profile, field)
            ):
                add(field)
        # starter progression
        for field in self._catalog.for_phase(product, IntakePhase.STARTER):
            if field.enabled and field.field_id not in session.declined_fields and self._field_needs_asking(session, profile, field):
                add(field)
        candidates.sort(key=lambda f: (f.priority, f.field_id))
        return candidates[0] if candidates else None

    def _question_payload(self, field: IntakeFieldDefinition) -> SafeQuestion:
        return SafeQuestion(
            field_id=field.field_id,
            canonical_path=self._catalog.resolve_template(field.canonical_path_template),
            question=field.question,
            short_label=field.short_label,
            input_type=field.input_type.value,
            sensitivity=field.sensitivity.value,
            intake_phase=field.intake_phase.value,
            choices=field.choices,
            help_text=field.help_text,
            workflow_status="awaiting_input",
        )

    def _terminal_status(self, session: IntakeSession) -> IntakeSessionStatus:
        if session.profile_id is None:
            return IntakeSessionStatus.ACTIVE
        profile = self._vault.get(session.profile_id)
        if profile is None:
            return IntakeSessionStatus.ACTIVE
        if profile.is_live_quote_ready:
            return IntakeSessionStatus.COMPLETE
        starter_missing = [
            f for f in self._catalog.for_phase(InsuranceType.AUTO, IntakePhase.STARTER)
            if self._field_missing(profile, f)
        ]
        if not starter_missing:
            return IntakeSessionStatus.STARTER_COMPLETE
        return IntakeSessionStatus.COLLECTING

    # --- validation (no side effects) ---------------------------------

    def validate_value(self, session_id: str, canonical_path: str, value: Any) -> dict[str, Any]:
        """Dry-run validation used by the LangGraph validate node.

        Never mutates the vault, session, pending units, or seed values.
        """
        session = self.get_session(session_id)
        safe_path = self._normalize_path(canonical_path)
        if session.status is IntakeSessionStatus.PRODUCT_REJECTED:
            return self._dry(safe_path, False, "product not implemented")
        field = self._catalog.by_path(safe_path)
        if field is None:
            return self._dry(safe_path, False, f"unsupported field path {safe_path!r}")
        if field.household_attestation_required and not self._consent.has_active(
            session.session_id, ConsentScope.HOUSEHOLD_DRIVER
        ):
            return self._dry(safe_path, False, "consent required before household driver data can be collected")
        if session.profile_id is None:
            if not field.seed_required:
                return self._dry(safe_path, False, "complete required starter fields first")
            error = _input_type_error(field, value)
            if error:
                return self._dry(safe_path, False, error)
            if field.field_id == "legal_name" and (not isinstance(value, str) or not value.strip()):
                return self._dry(safe_path, False, "legal name must not be empty")
            if field.field_id == "postal_code":
                try:
                    AddressInformation(province=Province.ON, postal_code=value)
                except ValidationError:
                    return self._dry(safe_path, False, "invalid postal code")
            return self._dry(safe_path, True, None, field.field_id)
        profile = self._vault.get(session.profile_id)
        if profile is None:
            return self._dry(safe_path, False, "profile not found")
        error = _input_type_error(field, value)
        if error:
            return self._dry(safe_path, False, error, field.field_id)
        if field.item_unit and field.item_unit_required and not self._container_exists(profile, field):
            # unit fields are canonical-validated at assembly time
            return self._dry(safe_path, True, None, field.field_id)
        try:
            profile.updated(safe_path, value)
        except (ProfileUpdateError, FieldPathError):
            return self._dry(safe_path, False, f"invalid value for canonical field path {safe_path!r}", field.field_id)
        return self._dry(safe_path, True, None, field.field_id)

    @staticmethod
    def _dry(path: str, ok: bool, error: Optional[str], field_id: Optional[str] = None) -> dict[str, Any]:
        return {
            "validation_success": ok,
            "error_message": error,
            "field_id": field_id,
            "canonical_path": path,
        }

    # --- submit answer ------------------------------------------------

    def submit_answer(self, session_id: str, canonical_path: str, value: Any) -> SubmitAnswerResult:
        session = self.get_session(session_id)
        safe_path = self._normalize_path(canonical_path)
        if session.status is IntakeSessionStatus.PRODUCT_REJECTED:
            return SubmitAnswerResult(
                session_id=session_id,
                canonical_path=safe_path,
                validation_success=False,
                error_message="product not implemented",
                workflow_status="product_rejected",
            )
        field = self._catalog.by_path(safe_path)
        if field is None:
            return SubmitAnswerResult(
                session_id=session_id,
                canonical_path=safe_path,
                validation_success=False,
                error_message=f"unsupported field path {safe_path!r}",
                workflow_status=session.status.value,
            )
        if field.household_attestation_required and not self._consent.has_active(
            session.session_id, ConsentScope.HOUSEHOLD_DRIVER
        ):
            return SubmitAnswerResult(
                session_id=session_id,
                field_id=field.field_id,
                canonical_path=safe_path,
                validation_success=False,
                error_message="consent required before household driver data can be collected",
                workflow_status=session.status.value,
            )
        if session.profile_id is None:
            if not field.seed_required:
                return SubmitAnswerResult(
                    session_id=session_id,
                    field_id=field.field_id,
                    canonical_path=safe_path,
                    validation_success=False,
                    error_message="complete required starter fields first",
                    workflow_status=session.status.value,
                )
            return self._submit_seed(session, field, safe_path, value)
        profile = self._vault.get(session.profile_id)
        if profile is None:
            return SubmitAnswerResult(
                session_id=session_id,
                canonical_path=safe_path,
                validation_success=False,
                error_message="profile not found",
                workflow_status="error",
            )
        error = _input_type_error(field, value)
        if error:
            return self._invalid(session, field, safe_path, error)
        if field.item_unit and field.item_unit_required:
            return self._submit_unit(session, profile, field, safe_path, value)
        try:
            new_profile = profile.updated(safe_path, value)
        except (ProfileUpdateError, FieldPathError) as exc:
            return self._invalid(session, field, safe_path, str(exc))
        self._vault.update(session.profile_id, new_profile)
        return self._after_success(session, field, safe_path)

    def _submit_seed(self, session: IntakeSession, field: IntakeFieldDefinition, safe_path: str, value: Any) -> SubmitAnswerResult:
        error = _input_type_error(field, value)
        if error:
            return self._invalid(session, field, safe_path, error)
        if field.field_id == "legal_name" and (not isinstance(value, str) or not value.strip()):
            return self._invalid(session, field, safe_path, "legal name must not be empty")
        if field.field_id == "postal_code":
            try:
                AddressInformation(province=Province.ON, postal_code=value)
            except ValidationError:
                return self._invalid(session, field, safe_path, "invalid postal code")
        self._seed_values.setdefault(session.session_id, {})[field.field_id] = value
        seed = self._seed_values[session.session_id]
        seed_fields = self._catalog.seed_fields(InsuranceType.AUTO)
        missing = [f.field_id for f in seed_fields if f.field_id not in seed]
        if missing:
            session = self._touch(session, current_field_id=field.field_id, current_canonical_path=safe_path)
            question = self._next_question(session)
            return SubmitAnswerResult(
                session_id=session.session_id,
                field_id=field.field_id,
                canonical_path=safe_path,
                validation_success=True,
                workflow_status=session.status.value,
                next_question=question,
            )
        profile = InsuranceProfile(
            insurance_type=InsuranceType.AUTO,
            consent=ConsentState(
                consent_timestamp=dt.datetime.now(dt.timezone.utc),
                quote_mode=QuoteMode.LIVE_QUOTE,
            ),
            applicant=ApplicantInformation(
                identity=ApplicantIdentity(legal_name=seed["legal_name"]),
                contact=ContactInformation(),
                address=AddressInformation(province=Province.ON, postal_code=seed["postal_code"]),
            ),
            product_data=AutoInsuranceProfile(),
        )
        profile_id = self._vault.create(profile)
        self._seed_values.pop(session.session_id, None)
        session = self._touch(
            session,
            profile_id=profile_id,
            status=IntakeSessionStatus.ACTIVE,
            current_field_id=None,
            current_canonical_path=None,
            completed_fields=[
                *session.completed_fields,
                "applicant.identity.legal_name",
                "applicant.address.postal_code",
            ],
        )
        question = self._next_question(session)
        return SubmitAnswerResult(
            session_id=session.session_id,
            field_id=field.field_id,
            canonical_path=safe_path,
            validation_success=True,
            workflow_status=session.status.value,
            next_question=question,
        )

    def _submit_unit(
        self,
        session: IntakeSession,
        profile: InsuranceProfile,
        field: IntakeFieldDefinition,
        safe_path: str,
        value: Any,
    ) -> SubmitAnswerResult:
        if self._container_exists(profile, field):
            try:
                new_profile = profile.updated(safe_path, value)
            except (ProfileUpdateError, FieldPathError) as exc:
                return self._invalid(session, field, safe_path, str(exc))
            self._vault.update(session.profile_id, new_profile)
            return self._after_success(session, field, safe_path)
        # accumulate pending unit values (in-memory; never serialized)
        pending = self._pending_units.setdefault(session.session_id, {}).setdefault(field.item_unit, {})
        pending[field.field_id] = value
        required = self._catalog.unit_fields(InsuranceType.AUTO, field.item_unit)
        missing = [f.field_id for f in required if f.field_id not in pending]
        if missing:
            session = self._touch(session, current_field_id=field.field_id, current_canonical_path=safe_path)
            question = self._next_question(session)
            return SubmitAnswerResult(
                session_id=session.session_id,
                field_id=field.field_id,
                canonical_path=safe_path,
                validation_success=True,
                workflow_status=session.status.value,
                next_question=question,
            )
        builder = _UNIT_BUILDERS.get(field.item_unit)
        if builder is None:
            self._pending_units.setdefault(session.session_id, {}).pop(field.item_unit, None)
            return self._invalid(session, field, safe_path, "unsupported item unit")
        try:
            item = builder(pending)
        except (ValidationError, KeyError, TypeError) as exc:
            self._pending_units.setdefault(session.session_id, {}).pop(field.item_unit, None)
            return self._invalid(session, field, safe_path, str(exc))
        container_path = self._catalog.container_path(field)
        existing = [e.model_dump(mode="python") for e in self._resolve_list(profile, container_path)]
        try:
            new_profile = profile.updated(container_path, [*existing, item])
        except (ProfileUpdateError, FieldPathError) as exc:
            self._pending_units.setdefault(session.session_id, {}).pop(field.item_unit, None)
            return self._invalid(session, field, safe_path, str(exc))
        self._vault.update(session.profile_id, new_profile)
        self._pending_units.setdefault(session.session_id, {}).pop(field.item_unit, None)
        completed = [self._catalog.resolve_template(r.canonical_path_template) for r in required]
        session = self._touch(
            session,
            completed_fields=list(dict.fromkeys([*session.completed_fields, *completed])),
            current_field_id=None,
            current_canonical_path=None,
        )
        question = self._next_question(session)
        return SubmitAnswerResult(
            session_id=session.session_id,
            field_id=field.field_id,
            canonical_path=safe_path,
            validation_success=True,
            workflow_status=session.status.value,
            next_question=question,
        )

    def _after_success(
        self, session: IntakeSession, field: IntakeFieldDefinition, safe_path: str
    ) -> SubmitAnswerResult:
        session = self._touch(
            session,
            completed_fields=list(dict.fromkeys([*session.completed_fields, safe_path])),
            current_field_id=None,
            current_canonical_path=None,
            invalid_retries={k: v for k, v in session.invalid_retries.items() if k != field.field_id},
        )
        question = self._next_question(session)
        return SubmitAnswerResult(
            session_id=session.session_id,
            field_id=field.field_id,
            canonical_path=safe_path,
            validation_success=True,
            workflow_status=session.status.value,
            next_question=question,
        )

    def _invalid(self, session: IntakeSession, field: IntakeFieldDefinition, safe_path: str, error: Any) -> SubmitAnswerResult:
        message = error if isinstance(error, str) else f"invalid value for canonical field path {safe_path!r}"
        retries = dict(session.invalid_retries)
        retries[field.field_id] = retries.get(field.field_id, 0) + 1
        session = self._touch(
            session,
            current_field_id=field.field_id,
            current_canonical_path=safe_path,
            invalid_retries=retries,
            validation_retry_count=session.validation_retry_count + 1,
        )
        question = self._next_question(session)
        return SubmitAnswerResult(
            session_id=session.session_id,
            field_id=field.field_id,
            canonical_path=safe_path,
            validation_success=False,
            error_message=message,
            retry_eligible=True,
            workflow_status=session.status.value,
            next_question=question,
        )

    # --- external field requests --------------------------------------

    def request_fields(
        self, session_id: str, requested_paths: list[str], source_context: Optional[str] = None
    ) -> list[FieldRequestOutcome]:
        session = self.get_session(session_id)
        outcomes: list[FieldRequestOutcome] = []
        for raw_path in requested_paths:
            concrete = self._normalize_path(raw_path)
            field = self._catalog.by_path(concrete)
            if field is None:
                outcomes.append(
                    FieldRequestOutcome(
                        requested_path=raw_path,
                        canonical_path=concrete,
                        state=FieldRequestState.UNSUPPORTED,
                        unsupported_reason="no catalog definition for this canonical path",
                        source_context=source_context,
                    )
                )
                continue
            if field.household_attestation_required and not self._consent.has_active(
                session.session_id, ConsentScope.HOUSEHOLD_DRIVER
            ):
                outcomes.append(
                    FieldRequestOutcome(
                        requested_path=raw_path,
                        canonical_path=concrete,
                        field_id=field.field_id,
                        state=FieldRequestState.UNKNOWN,
                        consent_required=True,
                        human_checkpoint_required=True,
                        checkpoint_kind="consent_attestation",
                        source_context=source_context,
                    )
                )
                continue
            already = False
            if session.profile_id is not None:
                profile = self._vault.get(session.profile_id)
                already = profile is not None and not self._field_missing(profile, field)
            if already:
                outcomes.append(
                    FieldRequestOutcome(
                        requested_path=raw_path,
                        canonical_path=concrete,
                        field_id=field.field_id,
                        state=FieldRequestState.ANSWERED,
                        already_known=True,
                        source_context=source_context,
                    )
                )
                continue
            if field.field_id not in session.requested_fields:
                session = self._touch(session, requested_fields=[*session.requested_fields, field.field_id])
            outcomes.append(
                FieldRequestOutcome(
                    requested_path=raw_path,
                    canonical_path=concrete,
                    field_id=field.field_id,
                    state=FieldRequestState.REQUESTED,
                    source_context=source_context,
                )
            )
        return outcomes

    def get_missing_requested_fields(self, session_id: str) -> list[str]:
        session = self.get_session(session_id)
        missing: list[str] = []
        for field_id in session.requested_fields:
            field = self._catalog.get(field_id)
            if field is None:
                continue
            if session.profile_id is None:
                if not self._seed_missing(session, field):
                    continue
            else:
                profile = self._vault.get(session.profile_id)
                if profile is not None and not self._field_missing(profile, field):
                    continue
            missing.append(field_id)
        return missing

    def decline_field(self, session_id: str, canonical_path: str) -> IntakeSession:
        session = self.get_session(session_id)
        field = self._catalog.by_path(self._normalize_path(canonical_path))
        if field is None or field.field_id in session.declined_fields:
            return session
        return self._touch(
            session,
            declined_fields=[*session.declined_fields, field.field_id],
            requested_fields=[f for f in session.requested_fields if f != field.field_id],
        )

    # --- safe profile summary -----------------------------------------

    def get_safe_profile_summary(self, session_id: str) -> ProfileSummary:
        session = self.get_session(session_id)
        fields: list[ProfileSummaryField] = []
        if session.profile_id is None:
            seed = self._seed_values.get(session.session_id, {})
            for field in self._catalog.enabled(InsuranceType.AUTO):
                fields.append(
                    ProfileSummaryField(
                        canonical_path=self._catalog.resolve_template(field.canonical_path_template),
                        field_id=field.field_id,
                        label=field.short_label,
                        sensitivity=field.sensitivity.value,
                        intake_phase=field.intake_phase.value,
                        has_value=field.field_id in seed,
                    )
                )
            done = sum(1 for f in fields if f.has_value)
            seed_done = all(
                self._catalog.get(f.field_id) and f.field_id in seed
                for f in self._catalog.seed_fields(InsuranceType.AUTO)
            )
            return ProfileSummary(
                profile_id=None,
                insurance_type=InsuranceType.AUTO,
                status=session.status.value,
                completed_field_count=done,
                missing_field_count=len(fields) - done,
                starter_complete=seed_done,
                live_quote_ready=False,
                fields=fields,
            )
        profile = self._vault.get(session.profile_id)
        if profile is None:
            return ProfileSummary(
                insurance_type=InsuranceType.AUTO,
                status=session.status.value,
            )
        for field in self._catalog.enabled(InsuranceType.AUTO):
            has_value = not self._field_missing(profile, field)
            fields.append(
                ProfileSummaryField(
                    canonical_path=self._catalog.resolve_template(field.canonical_path_template),
                    field_id=field.field_id,
                    label=field.short_label,
                    sensitivity=field.sensitivity.value,
                    intake_phase=field.intake_phase.value,
                    has_value=has_value,
                )
            )
        done = sum(1 for f in fields if f.has_value)
        starter_missing = [
            f
            for f in self._catalog.for_phase(InsuranceType.AUTO, IntakePhase.STARTER)
            if self._field_missing(profile, f)
        ]
        return ProfileSummary(
            profile_id=session.profile_id,
            insurance_type=InsuranceType.AUTO,
            status=session.status.value,
            completed_field_count=done,
            missing_field_count=len(fields) - done,
            starter_complete=not starter_missing,
            live_quote_ready=profile.is_live_quote_ready,
            fields=fields,
        )

    # --- route disclosure & consent -----------------------------------

    def create_route_disclosure(
        self, session_id: str, registry_id: str, paths: Optional[list[str]] = None
    ) -> RouteDataDisclosure:
        entry = self._registry.get_by_registry_id(registry_id)
        if entry is None:
            raise RouteNotFoundError(registry_id)
        session = self.get_session(session_id)
        profile = self._vault.get(session.profile_id) if session.profile_id else None
        definitions: list[IntakeFieldDefinition] = []
        if paths:
            for raw_path in paths:
                field = self._catalog.by_path(self._normalize_path(raw_path))
                if field is not None:
                    definitions.append(field)
        else:
            definitions = self._catalog.enabled(InsuranceType.AUTO)
        items: list[RouteDataDisclosureItem] = []
        if profile is not None:
            for field in definitions:
                if self._field_missing(profile, field):
                    continue
                items.append(
                    RouteDataDisclosureItem(
                        canonical_path=self._catalog.resolve_template(field.canonical_path_template),
                        label=field.short_label,
                        sensitivity=field.sensitivity,
                    )
                )
        sensitive_items = [item.canonical_path for item in items if item.sensitivity is FieldSensitivity.SENSITIVE]
        return RouteDataDisclosure(
            registry_id=registry_id,
            registry_name=entry.brand_or_program or entry.legal_underwriter or registry_id,
            items=items,
            sensitive_items=sensitive_items,
        )

    def grant_route_consent(
        self, session_id: str, registry_id: str, paths: list[str], granted: bool
    ) -> RouteConsentDecision:
        entry = self._registry.get_by_registry_id(registry_id)
        if entry is None:
            raise RouteNotFoundError(registry_id)
        session = self.get_session(session_id)
        existing = self._consent.route_consent(session_id, registry_id)
        if existing is not None:
            return RouteConsentDecision(
                registry_id=registry_id,
                consent_id=existing.consent_id,
                granted=existing.granted,
                excluded=not existing.granted,
                decided_at=existing.timestamp,
                already_decided=True,
            )
        receipt = self._consent.record(
            session,
            ConsentScope.ROUTE_DISCLOSURE,
            granted=granted,
            route_registry_id=registry_id,
            paths=paths,
            purpose=f"disclosure to {entry.brand_or_program or registry_id}",
        )
        return RouteConsentDecision(
            registry_id=registry_id,
            consent_id=receipt.consent_id,
            granted=granted,
            excluded=not granted,
            decided_at=receipt.timestamp,
        )

    def record_household_driver_consent(self, session_id: str, driver_label: str) -> ConsentReceipt:
        session = self.get_session(session_id)
        return self._consent.record(
            session,
            ConsentScope.HOUSEHOLD_DRIVER,
            granted=True,
            subject_reference=driver_label,
            purpose="applicant attests the listed household driver consented to collection/use/disclosure",
        )

    def record_collection_consent(self, session_id: str) -> ConsentReceipt:
        session = self.get_session(session_id)
        return self._consent.record(
            session,
            ConsentScope.COLLECTION,
            granted=True,
            purpose="permission to collect and store applicant information for this journey",
        )

    # --- checkpoints ---------------------------------------------------

    def evaluate_checkpoint(self, kind: HumanCheckpointKind):
        return self._checkpoints.evaluate(kind)

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _normalize_path(path: str) -> str:
        if "{" in path:
            return IntakeFieldCatalog.resolve_template(path)
        return path

    def _field_missing(self, profile: InsuranceProfile, field: IntakeFieldDefinition) -> bool:
        concrete = self._catalog.resolve_template(field.canonical_path_template)
        return is_missing(profile, concrete)

    def _field_needs_asking(self, session: IntakeSession, profile: InsuranceProfile, field: IntakeFieldDefinition) -> bool:
        """True when a field is missing AND not already collected into a pending
        unit buffer (unit fields are canonical-validated at assembly). Disabled
        fields are never asked."""
        if not field.enabled:
            return False
        if field.item_unit and field.item_unit_required:
            pending = self._pending_units.get(session.session_id, {}).get(field.item_unit, {})
            if field.field_id in pending:
                return False
        return self._field_missing(profile, field)

    def _seed_missing(self, session: IntakeSession, field: IntakeFieldDefinition) -> bool:
        return field.field_id not in self._seed_values.get(session.session_id, {})

    def _container_exists(self, profile: InsuranceProfile, field: IntakeFieldDefinition) -> bool:
        container = self._catalog.container_path(field)
        if container is None:
            return False
        try:
            values = resolve(profile, container)
            return isinstance(values, list) and len(values) > 0
        except FieldPathError:
            return False

    def _resolve_list(self, profile: InsuranceProfile, container_path: str) -> list[Any]:
        try:
            values = resolve(profile, container_path)
            return list(values) if values else []
        except FieldPathError:
            return []

    def _touch(self, session: IntakeSession, **updates: Any) -> IntakeSession:
        updated = session.model_copy(update={**updates, "updated_at": dt.datetime.now(dt.timezone.utc)})
        self._sessions.save(updated)
        return updated
