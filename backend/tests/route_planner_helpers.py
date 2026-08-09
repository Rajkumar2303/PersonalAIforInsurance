"""Shared helpers for Issue #6 route-planner tests.

All tests use a small, fully-synthetic registry + rate-source + requirements
environment in temp directories - hermetic and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from app.models.insurance.enums import InsuranceType
from app.services.deduplication import RateSourceDeduplicationService
from app.services.market_registry import MarketRegistryService
from app.services.route_planner.requirements import RequirementResolver

DEFAULT_REQUIREMENTS = [
    "applicant.identity.legal_name",
    "applicant.address.postal_code",
    "product_data.drivers[0].licence.licence_number",
    "product_data.vehicles[0].identity.vin",
]


def write_registry(tmp_path: Path, entries: list[dict]) -> Path:
    directory = tmp_path / "reg"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto.json").write_text(json.dumps({"records": entries}), encoding="utf-8")
    return directory


def write_rate_sources(tmp_path: Path, records: list[dict]) -> Path:
    directory = tmp_path / "rs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_rate_sources.json").write_text(json.dumps({"records": records}), encoding="utf-8")
    return directory


def write_requirements(tmp_path: Path, default: list[str], per_route: dict[str, list[str]]) -> Path:
    directory = tmp_path / "routes"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_route_requirements.json").write_text(
        json.dumps({"default": default, "per_route": per_route}), encoding="utf-8"
    )
    return directory


def entry(registry_id: str, **overrides: object) -> dict:
    """One synthetic AUTO market registry record."""
    base: dict = {
        "registry_id": registry_id,
        "product_type": "auto",
        "brand_or_program": registry_id,
        "distribution_type": "direct",
        "product_scope": "standard_PPA",
        "status": "discovered",
        "active": True,
    }
    base.update(overrides)
    return base


def rate_source(rate_source_id: str, **overrides: object) -> dict:
    """One synthetic distinct rate source record."""
    base: dict = {
        "distinct_rate_source_id": rate_source_id,
        "product_type": "auto",
        "insurer_group": "TEST-GROUP",
        "related_registry_ids": [],
        "deduplication_status": "unique",
        "confidence": "high",
    }
    base.update(overrides)
    return base


class StubProfileSource:
    """Deterministic fake for the planner's safe profile surface."""

    def __init__(
        self,
        *,
        insurance_type: InsuranceType = InsuranceType.AUTO,
        presence: Optional[dict[str, bool]] = None,
        consent: Optional[dict[str, bool]] = None,
        gates: Optional[dict[str, str]] = None,
        request_results: Optional[list] = None,
    ) -> None:
        self._insurance_type = insurance_type
        self._presence = presence or {}
        self._consent = consent or {}
        self._gates = gates or {}
        self._request_results = request_results or []
        self.requests: list[tuple[list[str], Optional[str]]] = []

    def get_session(self, session_id: str):
        return SimpleNamespace(session_id=session_id, insurance_type=self._insurance_type)

    def check_supported(self, session) -> bool:
        return session.insurance_type is InsuranceType.AUTO

    def field_presence(self, session_id: str, paths: list[str]) -> dict[str, bool]:
        return {path: self._presence.get(path, False) for path in paths}

    def has_route_consent(self, session_id: str, registry_id: str) -> bool:
        return self._consent.get(registry_id, False)

    def field_gate(self, session_id: str, canonical_path: str) -> str:
        return self._gates.get(canonical_path, "ok")

    def request_fields(self, session_id: str, paths: list[str], source_context: Optional[str] = None) -> list:
        self.requests.append((paths, source_context))
        return self._request_results


def make_planner(
    tmp_path: Path,
    entries: list[dict],
    rate_sources: Optional[list[dict]] = None,
    default_reqs: Optional[list[str]] = None,
    per_route_reqs: Optional[dict[str, list[str]]] = None,
    profile_source: Optional[StubProfileSource] = None,
):
    """Build a RoutePlanner over a temp synthetic registry + dedup + data."""
    from app.services.route_planner.planner import RoutePlanner

    registry = MarketRegistryService(registry_dir=write_registry(tmp_path, entries))
    dedup = RateSourceDeduplicationService(
        registry_service=registry,
        rate_sources_dir=write_rate_sources(tmp_path, rate_sources if rate_sources is not None else []),
    )
    requirements = RequirementResolver(
        requirements_dir=write_requirements(
            tmp_path,
            default_reqs if default_reqs is not None else DEFAULT_REQUIREMENTS,
            per_route_reqs or {},
        )
    )
    return RoutePlanner(
        registry=registry,
        dedup=dedup,
        requirements=requirements,
        profile_source=profile_source or StubProfileSource(),
    )


def make_integration_env(
    tmp_path: Path,
    entries: list[dict],
    rate_sources: Optional[list[dict]] = None,
    default_reqs: Optional[list[str]] = None,
    per_route_reqs: Optional[dict[str, list[str]]] = None,
):
    """Couple a REAL IntakeEngine (synthetic intake catalog) with a planner.

    Uses the synthetic intake catalog from ``intake_helpers`` so
    ``request_fields``/``submit_answer``/``field_gate`` work end-to-end.
    Returns ``(engine, planner)``.
    """
    from intake_helpers import standard_fields, write_catalog

    from app.services.intake.catalog import IntakeFieldCatalog
    from app.services.intake.consent import ConsentService
    from app.services.intake.engine import IntakeEngine
    from app.services.intake.session_store import InMemorySessionStore
    from app.services.intake.vault import InMemoryProfileVault
    from app.services.route_planner.planner import IntakeProfileSource, RoutePlanner

    catalog = IntakeFieldCatalog(catalog_dir=write_catalog(tmp_path, standard_fields()))
    registry = MarketRegistryService(registry_dir=write_registry(tmp_path, entries))
    engine = IntakeEngine(
        catalog=catalog,
        vault=InMemoryProfileVault(),
        sessions=InMemorySessionStore(),
        consent=ConsentService(),
        registry=registry,
    )
    dedup = RateSourceDeduplicationService(
        registry_service=registry,
        rate_sources_dir=write_rate_sources(tmp_path, rate_sources if rate_sources is not None else []),
    )
    requirements = RequirementResolver(
        requirements_dir=write_requirements(
            tmp_path,
            default_reqs if default_reqs is not None else DEFAULT_REQUIREMENTS,
            per_route_reqs or {},
        )
    )
    planner = RoutePlanner(
        registry=registry,
        dedup=dedup,
        requirements=requirements,
        profile_source=IntakeProfileSource(engine),
    )
    return engine, planner


def complete_starter(engine, session_id: str) -> None:
    """Materialize the profile and complete the driver+vehicle units so the
    default requirements (licence, VIN) are present."""
    from intake_helpers import SYNTHETIC_LEGAL_NAME, SYNTHETIC_LICENCE, SYNTHETIC_VIN, seed_profile

    seed_profile(engine, session_id)
    engine.submit_answer(session_id, "product_data.drivers[0].licence.name_on_licence", SYNTHETIC_LEGAL_NAME)
    engine.submit_answer(session_id, "product_data.drivers[0].licence.licence_number", SYNTHETIC_LICENCE)
    engine.submit_answer(session_id, "product_data.drivers[0].licence.expiry_date", "2030-12-31")
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.vin", SYNTHETIC_VIN)
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.model_year", 2022)
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.make", "TestMake")
    engine.submit_answer(session_id, "product_data.vehicles[0].identity.model", "TestModel")
