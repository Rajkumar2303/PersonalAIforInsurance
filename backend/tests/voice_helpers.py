"""Shared helpers for Issue #9 voice tests (hermetic, synthetic).

Wires a REAL IntakeEngine + RoutePlanner (synthetic registry/rate-source data)
with a ``VoiceEngine`` (mock transport + deterministic interpreter) and a
``RecoveryEngine`` whose consent source is the live Issue #5 intake engine.
All questions/answers are synthetic; NO real phone calls, NO LLM, NO external
API involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.models.insurance.enums import InsuranceType
from app.models.voice import (
    BrokerQuestion,
    BrokerQuestionKind,
    PhoneHandoffContext,
    VoiceLifecycleStatus,
    VoiceSession,
)
from app.services.intake.catalog import IntakeFieldCatalog
from app.services.intake.consent import ConsentService
from app.services.intake.engine import IntakeEngine
from app.services.intake.session_store import InMemorySessionStore
from app.services.intake.vault import InMemoryProfileVault
from app.services.market_registry import MarketRegistryService
from app.services.recovery.attempt_store import InMemoryAttemptStore
from app.services.recovery.engine import IntakeConsentSource, PlannerRouteSource, RecoveryEngine
from app.services.recovery.policy import RecoveryPolicyLoader
from app.services.route_planner.planner import IntakeProfileSource, RoutePlanner
from app.services.voice import (
    DeterministicBrokerQuestionInterpreter,
    InMemoryVoiceSessionStore,
    MockVoiceTransport,
    ScriptedBrokerSimulator,
    VoiceEngine,
    VoiceValueSource,
)
from intake_helpers import standard_fields, write_catalog
from route_planner_helpers import (
    DEFAULT_REQUIREMENTS,
    complete_starter,
    entry,
    make_planner,
    rate_source,
    write_rate_sources,
    write_registry,
)

VOICE_REGISTRY_ID = "voice-provider"
VOICE_PHONE = "1-800-MOCK-PROVIDER"


@dataclass
class VoiceEnv:
    engine: VoiceEngine
    store: InMemoryVoiceSessionStore
    transport: MockVoiceTransport
    interpreter: DeterministicBrokerQuestionInterpreter
    intake: IntakeEngine
    planner: RoutePlanner
    recovery: RecoveryEngine
    recovery_store: InMemoryAttemptStore
    catalog: IntakeFieldCatalog
    registry_id: str = VOICE_REGISTRY_ID
    session_id: str = ""
    profile_id: Optional[str] = None


def make_voice_env(
    tmp_path: Path,
    *,
    entries: Optional[list[dict]] = None,
    rate_sources: Optional[list[dict]] = None,
    catalog_fields: Optional[list[dict]] = None,
    extra_aliases: Optional[dict[str, str]] = None,
    events: Optional[list[BrokerQuestion]] = None,
    unreachable: bool = False,
    registry_id: str = VOICE_REGISTRY_ID,
    grant_consent: bool = True,
    retain_transcript: bool = False,
) -> VoiceEnv:
    """Build a hermetic voice environment (real intake + planner + recovery)."""
    entries = entries or [
        entry(
            registry_id,
            distinct_rate_source_id="RS-VOICE",
            public_phone_route=VOICE_PHONE,
            callback_route="1-800-MOCK-CALLBACK",
            quote_url=None,
        )
    ]
    rate_sources = rate_sources or [rate_source("RS-VOICE", related_registry_ids=[registry_id])]
    catalog_fields = catalog_fields or standard_fields()

    catalog = IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, catalog_fields))
    registry = MarketRegistryService(registry_dir=write_registry(tmp_path, entries))
    intake = IntakeEngine(
        catalog=catalog,
        vault=InMemoryProfileVault(),
        sessions=InMemorySessionStore(),
        consent=ConsentService(),
        registry=registry,
    )
    session, _gate = intake.create_session(InsuranceType.AUTO)
    complete_starter(intake, session.session_id)
    if grant_consent:
        intake.grant_route_consent(session.session_id, registry_id, [], True)

    planner = make_planner(
        tmp_path,
        entries,
        rate_sources,
        default_reqs=DEFAULT_REQUIREMENTS,
        per_route_reqs={},
        profile_source=IntakeProfileSource(intake),
    )

    store = InMemoryVoiceSessionStore()
    transport = MockVoiceTransport(
        ScriptedBrokerSimulator(events, unreachable=unreachable),
        retain_transcript=retain_transcript,
    )
    interpreter = DeterministicBrokerQuestionInterpreter(
        catalog=catalog, aliases=extra_aliases
    )
    recovery_store = InMemoryAttemptStore()
    loader = RecoveryPolicyLoader(policy_dir=tmp_path / "voice_recovery")
    recovery = RecoveryEngine(
        store=recovery_store,
        policy=loader.load(),
        route_source=PlannerRouteSource(planner),
        consent_source=IntakeConsentSource(intake),
    )
    voice = VoiceEngine(
        store=store,
        values=VoiceValueSource(intake),
        interpreter=interpreter,
        transport=transport,
        recovery=recovery,
    )
    return VoiceEnv(
        engine=voice,
        store=store,
        transport=transport,
        interpreter=interpreter,
        intake=intake,
        planner=planner,
        recovery=recovery,
        recovery_store=recovery_store,
        catalog=catalog,
        registry_id=registry_id,
        session_id=session.session_id,
        profile_id=session.profile_id,
    )


def make_handoff_context(
    env: VoiceEnv,
    *,
    callback_reason: Optional[str] = None,
    missing_paths: Optional[list[str]] = None,
    authorized_paths: Optional[list[str]] = None,
) -> PhoneHandoffContext:
    return PhoneHandoffContext(
        intake_session_id=env.session_id,
        registry_id=env.registry_id,
        distinct_rate_source_id="RS-VOICE",
        planned_route_id=f"route-{env.registry_id}",
        callback_reason=callback_reason,
        provider_phone_route=VOICE_PHONE,
        authorized_canonical_paths=authorized_paths or [],
        missing_canonical_paths=missing_paths or [],
    )


def prepare_and_disclose(env: VoiceEnv, **ctx_overrides: object) -> VoiceSession:
    """Prepare a voice session and grant automation disclosure (happy path)."""
    context = make_handoff_context(env, **ctx_overrides)
    session = env.engine.prepare_handoff(context)
    env.engine.disclose_automation(session.voice_session_id, granted=True)
    return env.engine.get(session.voice_session_id)


def field_question(path: str, *, kind: BrokerQuestionKind = BrokerQuestionKind.CANONICAL_FIELD) -> BrokerQuestion:
    """One structured canonical-field question (bypasses the interpreter)."""
    return BrokerQuestion(kind=kind, canonical_path=path, raw_safe_text=path, mapping_confidence=1.0)


def revoke_route_consent(env: VoiceEnv, registry_id: str) -> None:
    """Revoke the active route-disclosure consent for a registry (test-only).

    ``grant_route_consent`` is idempotent (returns ``already_decided``), so a
    genuine revocation must go through the Issue #5 consent service. This
    proves the VoiceEngine re-checks CURRENT consent live on resume.
    """
    receipt = env.intake._consent.route_consent(env.session_id, registry_id)
    if receipt is not None:
        env.intake._consent.revoke(receipt.consent_id)


def kind_question(kind: BrokerQuestionKind, *, path: Optional[str] = None, text: Optional[str] = None) -> BrokerQuestion:
    """One structured statement/checkpoint question by kind."""
    return BrokerQuestion(kind=kind, canonical_path=path, raw_safe_text=text or kind.value, mapping_confidence=1.0)


def scripted_happy_path_questions() -> list[BrokerQuestion]:
    """8 known insurance questions whose answers already exist in the
    Issue #5 profile vault after ``complete_starter`` (Prompt 2 unattended
    happy path), ending with a firm quote."""
    return [
        field_question("applicant.address.postal_code"),
        field_question("product_data.vehicles", kind=BrokerQuestionKind.COLLECTION_LENGTH),
        field_question("product_data.drivers", kind=BrokerQuestionKind.COLLECTION_LENGTH),
        field_question("product_data.vehicles[0].identity.model_year"),
        field_question("product_data.vehicles[0].identity.make"),
        field_question("product_data.vehicles[0].identity.model"),
        field_question("product_data.drivers[0].licence.expiry_date"),
        field_question("product_data.drivers[0].licence.name_on_licence"),
        kind_question(BrokerQuestionKind.QUOTE_DISCLOSURE, text="Your annual premium is ready."),
    ]
