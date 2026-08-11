"""Shared helpers for Issue #8.5 comparison/demo tests.

Builds a fully-isolated DEMO overlay (registry / route requirements / rate
sources / browser route config pointing at the ephemeral local mock site) and
drives the real intake engine with the canonical synthetic persona. This
mirrors how the web demo runs, but hermetic: temp dirs + ephemeral ports, no
internet, no LangSmith uploads, no real insurers.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.demo.mock_quote_site import build_mock_route_config
from app.demo.personas import standard_auto_persona
from app.demo.runtime import DemoRuntime
from app.models.insurance.enums import InsuranceType
from app.services.intake import get_intake_engine
from app.services.intake.catalog import IntakeFieldCatalog

MOCK_PRIMARY = "mock-insurer"
MOCK_ALT = "mock-insurer-broker"
MOCK_RATE_SOURCE = "RS-MOCK-INSURER"

DEFAULT_REQUIREMENTS = [
    "applicant.identity.legal_name",
    "applicant.address.postal_code",
    "product_data.drivers[0].licence.licence_number",
    "product_data.vehicles[0].identity.vin",
]


def mock_registry_entry(registry_id: str, site, *, brand: str, distribution: str) -> dict:
    return {
        "registry_id": registry_id,
        "product_type": "auto",
        "legal_underwriter": f"{brand} (synthetic)",
        "insurer_group": "Mock",
        "brand_or_program": brand,
        "distribution_type": distribution,
        "product_scope": "standard_PPA",
        "distinct_rate_source_id": MOCK_RATE_SOURCE,
        "quote_url": site.url("/page-a"),
        "requirements": ["licence", "vin"],
        "automation_notes": "SYNTHETIC DEMO ONLY - never live-verified.",
        "status": "discovered",
        "source_citation": "demo_overlay_test",
        "active": True,
    }


def write_demo_overlay(tmp_path: Path, site) -> Path:
    """Write a hermetic demo overlay; returns the demo_dir (tmp_path)."""
    reg = tmp_path / "market_registry"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / "auto.json").write_text(
        json.dumps(
            {
                "records": [
                    mock_registry_entry(MOCK_PRIMARY, site, brand="Mock Insurer", distribution="direct"),
                    mock_registry_entry(MOCK_ALT, site, brand="Mock Insurer Broker", distribution="broker"),
                ]
            }
        ),
        encoding="utf-8",
    )

    routes = tmp_path / "routes"
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "auto_route_requirements.json").write_text(
        json.dumps(
            {
                "default": DEFAULT_REQUIREMENTS,
                "per_route": {
                    MOCK_PRIMARY: ["product_data.vehicles[0].use.annual_kilometres"],
                    MOCK_ALT: ["product_data.vehicles[0].use.annual_kilometres"],
                },
            }
        ),
        encoding="utf-8",
    )

    rs = tmp_path / "rate_sources"
    rs.mkdir(parents=True, exist_ok=True)
    (rs / "auto_rate_sources.json").write_text(json.dumps({"records": []}), encoding="utf-8")

    browser = tmp_path / "browser" / "routes"
    browser.mkdir(parents=True, exist_ok=True)
    cfg = build_mock_route_config(start_url=site.url("/page-a"))
    (browser / f"{MOCK_PRIMARY}.json").write_text(
        json.dumps(cfg.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return tmp_path


def submit_persona(engine, session_id: str) -> None:
    """Submit the canonical synthetic persona in the engine-correct order."""
    catalog = IntakeFieldCatalog()
    persona = standard_auto_persona()

    def _sub(field) -> None:
        path = catalog.resolve_template(field.canonical_path_template)
        if path in persona:
            result = engine.submit_answer(session_id, path, persona[path])
            assert result.validation_success, (field.field_id, result.error_message)

    for field in catalog.enabled(InsuranceType.AUTO):
        if field.seed_required:
            _sub(field)
    for field in catalog.enabled(InsuranceType.AUTO):
        if field.item_unit and field.item_unit_required:
            _sub(field)
    for field in sorted(catalog.enabled(InsuranceType.AUTO), key=lambda f: (f.priority, f.field_id)):
        if field.seed_required or (field.item_unit and field.item_unit_required) or field.household_attestation_required:
            continue
        _sub(field)


def make_demo_env(tmp_path: Path, site, *, grant_consent: bool = True) -> tuple[DemoRuntime, str]:
    """Build a hermetic demo runtime + a complete AUTO intake session.

    Returns (runtime, session_id). The runtime shares the real intake engine's
    stores (sessions/vault/consent) but resolves route lookups against the
    isolated demo overlay.
    """
    demo_dir = write_demo_overlay(tmp_path, site)
    runtime = DemoRuntime(demo_dir=demo_dir, mock_site=site, headless=True)
    engine = get_intake_engine()
    session, _gate = engine.create_session(InsuranceType.AUTO)
    submit_persona(engine, session.session_id)
    if grant_consent:
        for rid in (MOCK_PRIMARY, MOCK_ALT):
            runtime.intake.grant_route_consent(session.session_id, rid, [], True)
    return runtime, session.session_id
