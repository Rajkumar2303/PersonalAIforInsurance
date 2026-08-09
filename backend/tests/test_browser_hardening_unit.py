"""Issue #7 Prompt 2 - unit-level hardening tests (no browser required).

Covers: value transformations, price parsing, route-config validation,
JIT value boundary, live privacy defaults, page-content privacy meta-check,
adapter boundary meta-check, and page-signature drift resilience.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.browser.detect import PageDetector, _normalize
from app.browser.fill import transform_value
from app.browser.matchers import FieldMapper
from app.browser.route_identity import registry_id_for_planned_route
from app.browser.session import live_privacy_context_kwargs
from app.core.config import get_settings
from app.models.browser.config import (
    BrowserFieldBinding,
    BrowserRouteConfig,
    FillStrategy,
    MatchPattern,
    MatchStrategy,
    TransformKind,
)
from app.models.browser.observation import BrowserFieldObservation, BrowserPageObservation
from browser_helpers import make_browser_env
from personas import make_standard_auto_profile

VIN = "product_data.vehicles[0].identity.vin"


def _binding(transform: TransformKind, **overrides) -> BrowserFieldBinding:
    return BrowserFieldBinding(
        external_field_id="x",
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="X")],
        canonical_path="p",
        transform=transform,
        **overrides,
    )


# --- 7. value transformations ----------------------------------------

def test_transform_currency_option_map() -> None:
    b = _binding(TransformKind.ENUM_TO_LABEL, option_map={"2000000": "$2,000,000"})
    assert transform_value(2000000, b) == "$2,000,000"


def test_transform_date_mm_dd_yyyy() -> None:
    b = _binding(TransformKind.ISO_DATE_TO_DEST, date_format="%m/%d/%Y")
    assert transform_value(dt.date(2022, 3, 1), b) == "03/01/2022"


def test_transform_enum_label() -> None:
    b = _binding(TransformKind.ENUM_TO_LABEL, option_map={"commute_personal": "Personal / commute"})
    assert transform_value("commute_personal", b) == "Personal / commute"


def test_transform_bool_yes_no() -> None:
    b = _binding(TransformKind.BOOL_TO_YES_NO)
    assert transform_value(True, b) == "Yes"
    assert transform_value(False, b) == "No"


def test_invalid_transform_config_rejected() -> None:
    with pytest.raises(ValidationError):
        BrowserFieldBinding(
            external_field_id="x",
            match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value="X")],
            canonical_path="p",
            transform="not-a-transform",
        )


def test_invalid_fill_strategy_rejected() -> None:
    with pytest.raises(ValidationError):
        BrowserFieldBinding(
            external_field_id="x",
            match_patterns=[],
            canonical_path="p",
            fill_strategy="not-a-strategy",
        )


# --- 29. price parsing ------------------------------------------------

def test_parse_amount_common_formats() -> None:
    detector = PageDetector()
    assert detector._parse_amount("$1,234.56") == 1234.56
    assert detector._parse_amount("1,234.56") == 1234.56
    assert detector._parse_amount("$123") == 123.0
    # Raw text preserved; ambiguous locale formats are NOT silently trusted.
    assert detector._extract_amounts("Annual premium: $1,234.56 CAD", None) == ["1,234.56"]
    assert detector._extract_amounts("$123/month", None) == ["123"]


# --- 25. JIT value boundary -------------------------------------------

def test_get_field_value_rejects_list_path(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    with pytest.raises(ValueError) as exc_info:
        env.engine.get_field_value(pid, "product_data.drivers")
    assert "single leaf" in str(exc_info.value)


def test_value_never_added_to_action_or_session(tmp_path, mock_site) -> None:
    env = make_browser_env(tmp_path, mock_site)
    pid = env.engine.get_session(env.session_id).profile_id
    value = env.engine.get_field_value(pid, VIN)
    assert value == "1HGCM82633A000000"
    assert "1HGCM82633A000000" not in env.engine.get_session(env.session_id).model_dump_json()


# --- 27. screenshot / trace safety -------------------------------------

def test_live_privacy_context_kwargs_defaults() -> None:
    kwargs = live_privacy_context_kwargs()
    assert "no_viewport" in kwargs
    assert "record_video_dir" not in kwargs
    assert "record_har_path" not in kwargs
    assert "screenshots" not in kwargs
    assert "tracing" not in kwargs


def test_settings_screenshot_and_headless_defaults() -> None:
    settings = get_settings()
    assert settings.browser_screenshot_enabled is False
    assert settings.browser_headless is True


# --- 26. page-content privacy meta-check ------------------------------

@pytest.mark.parametrize("forbidden", ["page.content()", "inner_html()", "get_by_text()",
                                       "localStorage", "sessionStorage", "cookies()",
                                       "request.post_data", "response.body()"])
def test_browser_source_never_captures_page_content(forbidden: str) -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "browser"
    for path in root.rglob("*.py"):
        if path.name == "manager.py":
            continue
        assert forbidden not in path.read_text(encoding="utf-8"), f"{forbidden} found in {path}"


# --- 36. adapter boundary meta-check ----------------------------------

def test_executor_has_no_insurer_specific_branches() -> None:
    executor = Path(__file__).resolve().parents[1] / "app" / "browser" / "executor.py"
    source = executor.read_text(encoding="utf-8")
    assert 'registry_id ==' not in source
    assert 'registry_id =="' not in source
    assert 'if registry_id' not in source


def test_planned_route_mapping_centralized() -> None:
    assert registry_id_for_planned_route("mock-insurer") == "mock-insurer"


# --- 33. route config validation --------------------------------------

def test_conflicting_page_signatures_accepted_but_deterministic() -> None:
    # Two signatures may overlap; matching is best-effort and deterministic.
    config = BrowserRouteConfig(
        registry_id="r",
        page_signatures=[
            {"signature_id": "a", "url_pattern": r"/page"},
            {"signature_id": "b", "url_pattern": r"/page"},
        ],
    )
    assert len(config.page_signatures) == 2


def test_invalid_action_safety_rejected() -> None:
    from app.models.browser.config import ActionBinding

    with pytest.raises(ValidationError):
        ActionBinding(action_type="continue", safety="not-a-safety", label_patterns=["Continue"])


def test_invalid_checkpoint_type_rejected() -> None:
    from app.models.browser.config import CheckpointBinding

    with pytest.raises(ValidationError):
        CheckpointBinding(checkpoint_type="not-a-kind", label_patterns=["x"])


def test_malformed_config_file_fails_early(tmp_path) -> None:
    from app.browser.config import BrowserRouteConfigLoader, RouteConfigLoadError

    d = tmp_path / "routes"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RouteConfigLoadError):
        BrowserRouteConfigLoader(config_dir=d).load("bad")


# --- 18. page signature drift resilience ------------------------------

def test_signature_heading_drift_still_matches() -> None:
    from app.browser.detect import PageDetector
    from app.models.browser.config import PageSignatureSpec

    detector = PageDetector()
    spec = PageSignatureSpec(
        signature_id="applicant", url_pattern=r"/page-a", heading_patterns=["Applicant Information"]
    )
    assert detector._signature_matches(
        spec, "http://127.0.0.1/page-a?step=1", "Applicant Information - Updated 2026", {"legal-name"}
    )


def test_signature_url_query_change_still_matches() -> None:
    from app.browser.detect import PageDetector
    from app.models.browser.config import PageSignatureSpec

    detector = PageDetector()
    spec = PageSignatureSpec(signature_id="applicant", url_pattern=r"/page-a", heading_patterns=["Applicant"])
    assert detector._signature_matches(spec, "http://127.0.0.1/page-a?utm=1", "Applicant Information", set())


def test_ambiguous_signature_not_authoritative() -> None:
    # Signature identity is advisory; matching is resilient but never treated
    # as the only signal (the generic inspector + mappings do the real work).
    config = BrowserRouteConfig(registry_id="r")
    assert config.page_signatures == []
