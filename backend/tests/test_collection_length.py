"""Issue #7 / Smoke #4 - generic derived collection-count transform tests.

Proves a binding with ``transform=collection_length`` derives its value from
the canonical profile collection length (``product_data.vehicles`` ->
``len(...)``, ``product_data.drivers`` -> ``len(...)``) and can never drift
from the actual list length - no separate stored count fields, no
insurer-specific code, fully config-driven (a future website asking "How many
vehicles? / How many drivers?" reuses the same transform).

Hermetic: local mock site only; no external network, no LLM, no LangSmith.
"""

from __future__ import annotations

import pytest

from app.browser.fill import FieldFiller, transform_value
from app.browser.inspect import PageInspector
from app.browser.manager import BrowserManager
from app.browser.mock_site import MOCK_REGISTRY_ID, build_mock_route_config
from app.browser.matchers import FieldMapper
from app.browser.value_provider import IntakeValueSource
from app.models.browser.config import (
    ActionBinding,
    BrowserFieldBinding,
    BrowserRouteConfig,
    FillStrategy,
    MatchPattern,
    MatchStrategy,
    PageSignatureSpec,
    TransformKind,
)
from app.models.browser.session import BrowserActionSafety, BrowserExecutionMode
from app.models.intake.field_catalog import FieldSensitivity
from browser_helpers import make_browser_env
from factories import make_vehicle
from personas import make_standard_auto_profile

VEHICLES_PATH = "product_data.vehicles"
DRIVERS_PATH = "product_data.drivers"


def _count_binding(external_id: str, canonical: str) -> BrowserFieldBinding:
    return BrowserFieldBinding(
        external_field_id=external_id,
        match_patterns=[MatchPattern(strategy=MatchStrategy.ID, value=external_id)],
        canonical_path=canonical,
        control_type="input",
        fill_strategy=FillStrategy.TEXT,
        transform=TransformKind.COLLECTION_LENGTH,
        required=True,
        sensitivity=FieldSensitivity.NON_SENSITIVE,
    )


def _counts_config(site) -> BrowserRouteConfig:
    """Route config pointing at /counts with ONLY the derived count bindings."""
    config = build_mock_route_config(MOCK_REGISTRY_ID, start_url=site.url("/counts"))
    return config.model_copy(
        update={
            "page_signatures": [
                PageSignatureSpec(
                    signature_id="counts",
                    url_pattern=r"/counts",
                    heading_patterns=["Vehicles & Drivers"],
                )
            ],
            "field_bindings": [
                _count_binding("vehicles-count", VEHICLES_PATH),
                _count_binding("drivers-count", DRIVERS_PATH),
            ],
            "action_bindings": [
                ActionBinding(
                    action_type="continue",
                    safety=BrowserActionSafety.SAFE_NAVIGATION,
                    label_patterns=["Continue"],
                )
            ],
        }
    )


def _two_vehicle_profile():
    profile = make_standard_auto_profile()
    auto = profile.product_data
    return profile.model_copy(
        update={"product_data": auto.model_copy(update={"vehicles": [*auto.vehicles, make_vehicle()]})}
    )


async def test_transform_collection_length_to_string() -> None:
    binding = _count_binding("vehicles-count", VEHICLES_PATH)
    assert transform_value(1, binding) == "1"
    assert transform_value(2, binding) == "2"
    assert transform_value(0, binding) == "0"


async def test_engine_collection_length_derives_from_profile(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    assert env.engine.get_collection_length(pid, VEHICLES_PATH) == 1
    assert env.engine.get_collection_length(pid, DRIVERS_PATH) == 1


async def test_engine_collection_length_cannot_drift_from_actual_list(tmp_path, mock_site) -> None:
    # A profile with TWO vehicles must derive 2 (and still 1 driver).
    env = make_browser_env(tmp_path, mock_site, persona=_two_vehicle_profile())
    pid = env.engine.get_session(env.session_id).profile_id
    assert env.engine.get_collection_length(pid, VEHICLES_PATH) == 2
    assert env.engine.get_collection_length(pid, DRIVERS_PATH) == 1


async def test_engine_collection_length_rejects_non_collection(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    with pytest.raises(ValueError):
        env.engine.get_collection_length(pid, "applicant.identity.legal_name")


async def test_value_source_collection_length(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    source = IntakeValueSource(env.engine)
    assert source.collection_length(env.session_id, VEHICLES_PATH) == 1
    assert source.collection_length(env.session_id, DRIVERS_PATH) == 1


async def test_collection_length_maps_and_fills_from_profile(tmp_path, mock_site) -> None:
    """The /counts inputs are mapped and filled with the DERIVED counts."""
    config = _counts_config(mock_site)
    env = make_browser_env(tmp_path, mock_site, route_config=config)
    source = IntakeValueSource(env.engine)
    browser = BrowserManager(headless=True)
    await browser.start()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(mock_site.url("/counts"), wait_until="domcontentloaded")
        await page.wait_for_timeout(150)
        obs = await PageInspector().inspect(page, 0)
        matched, _unmatched = FieldMapper().map(obs, config)
        by_id = {m.binding.external_field_id: m for m in matched}
        assert "vehicles-count" in by_id and "drivers-count" in by_id
        filler = FieldFiller()
        for ext_id, path in (("vehicles-count", VEHICLES_PATH), ("drivers-count", DRIVERS_PATH)):
            value = source.collection_length(env.session_id, path)
            await filler.fill(page, by_id[ext_id].observation, by_id[ext_id].binding, value)
            assert await page.locator(f"#{ext_id}").input_value() == "1"
    finally:
        await browser.stop()


async def test_collection_length_fill_derives_two_vehicles(tmp_path, mock_site) -> None:
    """A two-vehicle profile fills the count input with 2 (no drift from list)."""
    env = make_browser_env(tmp_path, mock_site, persona=_two_vehicle_profile())
    config = _counts_config(mock_site)
    source = IntakeValueSource(env.engine)
    browser = BrowserManager(headless=True)
    await browser.start()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(mock_site.url("/counts"), wait_until="domcontentloaded")
        await page.wait_for_timeout(150)
        obs = await PageInspector().inspect(page, 0)
        matched, _ = FieldMapper().map(obs, config)
        by_id = {m.binding.external_field_id: m for m in matched}
        value = source.collection_length(env.session_id, VEHICLES_PATH)
        assert value == 2
        await FieldFiller().fill(page, by_id["vehicles-count"].observation, by_id["vehicles-count"].binding, value)
        assert await page.locator("#vehicles-count").input_value() == "2"
    finally:
        await browser.stop()


async def test_executor_collection_length_workflow_reaches_quote(tmp_path, mock_site) -> None:
    """The real executor fills the derived counts and advances to the quote."""
    from app.graph.browser_workflow import build_browser_workflow

    config = _counts_config(mock_site)
    env = make_browser_env(tmp_path, mock_site, route_config=config)
    bs = env.manager.create(env.session_id, MOCK_REGISTRY_ID, BrowserExecutionMode.SANDBOX)
    try:
        await build_browser_workflow(env.manager).ainvoke(
            {"entry": "run", "browser_session_id": bs.browser_session_id, "max_steps": 15}
        )
        session = env.manager.get(bs.browser_session_id)
        assert session.quote_present is True
        assert "vehicles-count" in session.observed_field_ids
        assert "drivers-count" in session.observed_field_ids
    finally:
        try:
            await env.manager.close(bs.browser_session_id)
        except Exception:
            pass
        try:
            await env.browser_manager.stop()
        except Exception:
            pass
