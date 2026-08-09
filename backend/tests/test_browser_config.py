"""Issue #7 - route config validation, field mapping, transforms, action safety."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.browser.actions import ActionClassifier
from app.browser.fill import FillError, transform_value
from app.browser.matchers import FieldMapper
from app.models.browser.config import (
    BrowserFieldBinding,
    BrowserRouteConfig,
    FillStrategy,
    MatchPattern,
    MatchStrategy,
    TransformKind,
)
from app.models.browser.observation import BrowserFieldObservation, BrowserPageObservation
from app.models.browser.session import BrowserActionSafety


def _binding(external_id: str, label: str, canonical: str, **overrides) -> BrowserFieldBinding:
    return BrowserFieldBinding(
        external_field_id=external_id,
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_TEXT, value=label)],
        canonical_path=canonical,
        **overrides,
    )


def test_route_config_validation() -> None:
    config = BrowserRouteConfig(registry_id="mock-insurer", allowed_hosts=["127.0.0.1"])
    assert config.config_version == 1
    assert config.registry_id == "mock-insurer"


def test_route_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        BrowserRouteConfig.model_validate({"registry_id": "x", "unknown": 1})


def test_route_config_requires_registry_id() -> None:
    with pytest.raises(ValidationError):
        BrowserRouteConfig.model_validate({})


def test_field_mapper_maps_by_label() -> None:
    page = BrowserPageObservation(
        fields=[BrowserFieldObservation(external_field_id="legal-name", control_type="input", label="Legal name")]
    )
    config = BrowserRouteConfig(
        registry_id="r",
        field_bindings=[_binding("legal-name", "Legal name", "applicant.identity.legal_name")],
    )
    matched, unmatched = FieldMapper().map(page, config)
    assert len(matched) == 1
    assert matched[0].canonical_path == "applicant.identity.legal_name"
    assert unmatched == []


def test_field_mapper_label_contains() -> None:
    page = BrowserPageObservation(
        fields=[BrowserFieldObservation(external_field_id="annual-km", control_type="input",
                                        label="Approximate annual distance driven")]
    )
    binding = BrowserFieldBinding(
        external_field_id="annual-km",
        match_patterns=[MatchPattern(strategy=MatchStrategy.LABEL_CONTAINS, value="annual distance")],
        canonical_path="product_data.vehicles[0].use.annual_kilometres",
    )
    matched, _ = FieldMapper().map(page, BrowserRouteConfig(registry_id="r", field_bindings=[binding]))
    assert matched and matched[0].canonical_path == "product_data.vehicles[0].use.annual_kilometres"


def test_field_mapper_unmatched_stays_unknown() -> None:
    page = BrowserPageObservation(
        fields=[BrowserFieldObservation(external_field_id="shoe-size", control_type="input", label="Shoe size")]
    )
    matched, unmatched = FieldMapper().map(page, BrowserRouteConfig(registry_id="r", field_bindings=[]))
    assert matched == []
    assert len(unmatched) == 1
    assert unmatched[0].external_field_id == "shoe-size"


def test_transform_integer_to_string() -> None:
    binding = _binding("x", "X", "p", transform=TransformKind.INTEGER_TO_STRING)
    assert transform_value(12000, binding) == "12000"


def test_transform_bool_to_yes_no() -> None:
    binding = _binding("x", "X", "p", transform=TransformKind.BOOL_TO_YES_NO)
    assert transform_value(True, binding) == "Yes"
    assert transform_value(False, binding) == "No"


def test_transform_iso_date_to_dest() -> None:
    binding = _binding("x", "X", "p", transform=TransformKind.ISO_DATE_TO_DEST, date_format="%d/%m/%Y")
    assert transform_value(dt.date(1990, 1, 1), binding) == "01/01/1990"


def test_transform_enum_to_label() -> None:
    binding = _binding("x", "X", "p", transform=TransformKind.ENUM_TO_LABEL,
                       option_map={"english": "English"})
    assert transform_value("english", binding) == "English"
    assert transform_value("french", binding) == "french"


def test_transform_invalid_date_raises_safe_error() -> None:
    binding = _binding("x", "X", "p", transform=TransformKind.ISO_DATE_TO_DEST)
    with pytest.raises(FillError) as exc_info:
        transform_value("not-a-date", binding)
    assert "invalid date value" in str(exc_info.value)
    assert "1990" not in str(exc_info.value)


def _merged(config: BrowserRouteConfig) -> BrowserRouteConfig:
    """Apply the generic adapter's safe defaults (as the executor does)."""
    from app.browser.adapters import GenericQuoteSiteAdapter

    return GenericQuoteSiteAdapter().merged_config(config)


def test_action_classifier_safe_navigation() -> None:
    clickable = ActionClassifier().classify("Continue", _merged(BrowserRouteConfig(registry_id="r")))
    assert clickable.safety is BrowserActionSafety.SAFE_NAVIGATION
    assert clickable.action_type == "continue"


def test_action_classifier_human_checkpoint() -> None:
    clickable = ActionClassifier().classify("Verify identity", _merged(BrowserRouteConfig(registry_id="r")))
    assert clickable.safety is BrowserActionSafety.HUMAN_CHECKPOINT
    assert clickable.checkpoint is not None


def test_action_classifier_prohibited_purchase() -> None:
    clickable = ActionClassifier().classify("Buy Now", _merged(BrowserRouteConfig(registry_id="r")))
    assert clickable.safety is BrowserActionSafety.PROHIBITED
    assert clickable.checkpoint is not None and clickable.checkpoint.must_not_automate


def test_action_classifier_unknown_not_clicked() -> None:
    clickable = ActionClassifier().classify("Something unrelated", _merged(BrowserRouteConfig(registry_id="r")))
    assert clickable.safety is None
