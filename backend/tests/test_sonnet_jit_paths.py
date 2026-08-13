"""Audit every sonnet.json canonical path against the production JIT accessor.

Final pre-live verification (requirement 4): the earlier report inconsistently
showed ``product_data.vehicles`` / ``vehicles[0]...`` / ``product_data.drivers`` /
``drivers[0]...`` / ``coverage...``. This test proves that every one of the 18
``field_bindings`` in the REAL ``sonnet.json`` uses a FULLY-QUALIFIED canonical
path (``applicant.*`` or ``product_data.*``) and that each one resolves through
the SAME production just-in-time accessor the browser executor uses
(``IntakeEngine.get_field_value`` for scalars, ``IntakeEngine.get_collection_length``
for the two derived count bindings).

Hermetic: local mock site fixture only (no real Sonnet, no LLM, no LangSmith).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.browser.config import BrowserRouteConfigLoader
from app.models.browser.config import TransformKind

from browser_helpers import make_browser_env

REAL_ROUTES_DIR = Path(__file__).resolve().parents[1] / "data" / "browser" / "routes"

# The 16 scalar bindings and their exact synthetic values (from the standard
# auto persona used by make_browser_env). Asserted ONLY in test code - never
# logged, traced, or serialized by the application.
EXPECTED_SCALAR_VALUES: dict[str, object] = {
    "applicant.address.province": "ON",
    "product_data.vehicles[0].identity.model_year": 2022,
    "product_data.vehicles[0].identity.make": "TestMake",
    "product_data.vehicles[0].identity.model": "TestModel",
    "product_data.vehicles[0].identity.vin": "1HGCM82633A000000",
    "product_data.vehicles[0].use.annual_kilometres": 12000,
    "product_data.vehicles[0].use.carpool": False,
    "product_data.vehicles[0].use.one_way_commute_distance_km": 18,
    "product_data.vehicles[0].risk.winter_tires": True,
    "product_data.coverage.third_party_liability.selected_limit": 2_000_000,
    "applicant.address.postal_code": "M0A 0A0",
    "applicant.identity.date_of_birth": dt.date(1990, 1, 1),
    "applicant.identity.legal_name": "Test Applicant",
    "product_data.drivers[0].licence.licence_number": "T0000-00000-00000",
    "product_data.drivers[0].licence.name_on_licence": "Test Applicant",
    "product_data.drivers[0].licence.expiry_date": dt.date(2030, 12, 31),
}

# The two derived-count bindings -> expected collection lengths.
EXPECTED_COLLECTION_LENGTHS: dict[str, int] = {
    "product_data.vehicles": 1,
    "product_data.drivers": 1,
}


def _load_sonnet_config():
    return BrowserRouteConfigLoader(config_dir=REAL_ROUTES_DIR).load("sonnet")


def test_sonnet_config_has_exactly_18_bindings() -> None:
    cfg = _load_sonnet_config()
    assert len(cfg.field_bindings) == 18
    assert len({b.external_field_id for b in cfg.field_bindings}) == 18


def test_sonnet_all_18_canonical_paths_are_fully_qualified() -> None:
    """No report shorthand (coverage..., vehicles[0]..., drivers[0]...)."""
    cfg = _load_sonnet_config()
    for binding in cfg.field_bindings:
        path = binding.canonical_path
        assert path.startswith("applicant.") or path.startswith("product_data."), path
        # Bare-relative shorthands must never appear.
        assert not path.startswith("vehicles"), path
        assert not path.startswith("drivers"), path
        assert not path.startswith("coverage"), path
        assert not path.startswith("[0]"), path


def test_sonnet_collection_and_scalar_split_is_exactly_2_and_16() -> None:
    cfg = _load_sonnet_config()
    counts = [b for b in cfg.field_bindings if b.transform is TransformKind.COLLECTION_LENGTH]
    scalars = [b for b in cfg.field_bindings if b.transform is not TransformKind.COLLECTION_LENGTH]
    assert {b.canonical_path for b in counts} == set(EXPECTED_COLLECTION_LENGTHS)
    assert len(scalars) == 16
    assert len(counts) == 2


def test_sonnet_all_18_bindings_resolve_through_production_jit_accessor(tmp_path, mock_site) -> None:
    """Every binding resolves through the SAME JIT accessor the executor uses."""
    env = make_browser_env(tmp_path, mock_site, registry_id="sonnet")
    profile_id = env.engine.get_session(env.session_id).profile_id
    assert profile_id is not None
    cfg = _load_sonnet_config()

    # 1) collection bindings -> derived counts via get_collection_length.
    for path, expected in EXPECTED_COLLECTION_LENGTHS.items():
        assert env.engine.get_collection_length(profile_id, path) == expected

    # 2) scalar bindings -> single leaf values via get_field_value.
    for path, expected in EXPECTED_SCALAR_VALUES.items():
        value = env.engine.get_field_value(profile_id, path)
        assert value is not None, path
        assert not isinstance(value, (list, dict, tuple, set)), path
        assert value == expected, path

    # 3) accessor contract: scalars reject collections and vice versa.
    with pytest.raises(ValueError):
        env.engine.get_field_value(profile_id, "product_data.vehicles")
    with pytest.raises(ValueError):
        env.engine.get_field_value(profile_id, "product_data.drivers")
    with pytest.raises(ValueError):
        env.engine.get_collection_length(profile_id, "product_data.vehicles[0].identity.vin")


def test_sonnet_every_binding_jit_retrieval_matches_executor_value_source(tmp_path, mock_site) -> None:
    """Cross-check: the executor's IntakeValueSource yields the same values as
    the raw engine accessor for every scalar binding (constant/transform-aware).

    This mirrors exactly what ``BrowserExecutor._fill_known`` calls at fill
    time - constant_value is filled directly (never retrieved), collection
    lengths via ``collection_length``, everything else via ``get``.
    """
    from app.browser.value_provider import IntakeValueSource

    env = make_browser_env(tmp_path, mock_site, registry_id="sonnet")
    source = IntakeValueSource(env.engine)
    cfg = _load_sonnet_config()

    for binding in cfg.field_bindings:
        if binding.constant_value is not None:
            # Province=Ontario: a NON-PII route constant - never retrieved.
            assert binding.canonical_path == "applicant.address.province"
            assert binding.constant_value == "Ontario"
            assert source.get(env.session_id, binding.canonical_path) == "ON"  # still resolvable
        elif binding.transform is TransformKind.COLLECTION_LENGTH:
            assert source.collection_length(env.session_id, binding.canonical_path) == EXPECTED_COLLECTION_LENGTHS[binding.canonical_path]
        else:
            value = source.get(env.session_id, binding.canonical_path)
            assert value == EXPECTED_SCALAR_VALUES[binding.canonical_path], binding.canonical_path
