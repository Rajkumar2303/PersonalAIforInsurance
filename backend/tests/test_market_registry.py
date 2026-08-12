"""Tests for the data-driven Ontario market registry (Issue #3 Part B).

All tests are hermetic: they load either the checked-in seed or synthetic JSON
written to a temporary directory. No network, no LangSmith, no LLM calls.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from pydantic import ValidationError

from app.models.insurance.enums import InsuranceType
from app.models.registry import (
    DistributionType,
    MarketRegistryEntry,
    MarketRequirement,
    ProductScope,
    RegistryStatus,
)
from app.services.market_registry import MarketRegistryService, RegistryLoadError

# Quote-attempt terminal statuses (a LATER issue) - must stay distinct from the
# registry lifecycle statuses.
QUOTE_TERMINAL_STATUSES = {
    "quoted_comparable", "quoted_non_comparable", "estimate_only", "callback_required",
    "manual_handoff", "ineligible", "affinity_restricted", "specialty_only",
    "duplicate_rate_source", "not_currently_writing", "blocked", "unreachable", "unresolved",
}


def _record(registry_id: str = "synth-1", **overrides) -> dict:
    base = {
        "registry_id": registry_id,
        "product_type": "auto",
        "brand_or_program": "Synthetic Broker",
        "distribution_type": "broker",
        "product_scope": "standard_PPA",
        "requirements": ["licence", "vin"],
        "status": "discovered",
        "source_citation": "test",
    }
    base.update(overrides)
    return base


def _write_registry(tmp_path, records, filename: str = "auto.json"):
    directory = tmp_path / "market_registry"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps({"records": records}, indent=2), encoding="utf-8")
    return directory


# --- seed loading / validation ----------------------------------------

def test_registry_seed_loads_and_validates() -> None:
    service = MarketRegistryService()
    assert len(service.list_markets()) > 0
    for entry in service.list_markets():
        assert isinstance(entry, MarketRegistryEntry)


def test_registry_id_unique_in_seed() -> None:
    ids = [entry.registry_id for entry in MarketRegistryService().list_markets()]
    assert len(ids) == len(set(ids))


def test_product_type_valid_and_auto_only() -> None:
    service = MarketRegistryService()
    for entry in service.list_markets():
        assert entry.product_type in InsuranceType
        assert entry.product_type is InsuranceType.AUTO


def test_no_fabricated_future_product_entries() -> None:
    service = MarketRegistryService()
    for entry in service.list_markets():
        assert entry.product_type is InsuranceType.AUTO
    # Structurally support is present, but no HOME/TENANT/LIFE/TRAVEL/OTHER records.
    for product in (InsuranceType.HOME, InsuranceType.TENANT, InsuranceType.LIFE,
                    InsuranceType.TRAVEL, InsuranceType.OTHER):
        assert service.filter_by_product_type(product) == []


# --- enum validation ---------------------------------------------------

def test_distribution_type_enum_validation() -> None:
    MarketRegistryEntry.model_validate(_record(distribution_type="direct"))
    with pytest.raises(ValidationError):
        MarketRegistryEntry.model_validate(_record(distribution_type="not_a_type"))


def test_product_scope_enum_validation() -> None:
    MarketRegistryEntry.model_validate(_record(product_scope="collector"))
    with pytest.raises(ValidationError):
        MarketRegistryEntry.model_validate(_record(product_scope="everything"))


def test_requirement_enum_validation() -> None:
    MarketRegistryEntry.model_validate(_record(requirements=["licence", "human"]))
    with pytest.raises(ValidationError):
        MarketRegistryEntry.model_validate(_record(requirements=["licence", "bogus"]))


def test_requirements_normalized_deterministic() -> None:
    entry = MarketRegistryEntry.model_validate(_record(requirements=["vin", "licence", "vin"]))
    assert [r.value for r in entry.requirements] == ["licence", "vin"]


# --- queries -----------------------------------------------------------

def test_get_by_registry_id() -> None:
    service = MarketRegistryService()
    assert service.get_by_registry_id("sonnet").brand_or_program == "Sonnet"
    assert service.get_by_registry_id("does-not-exist") is None


def test_filter_by_product_type() -> None:
    service = MarketRegistryService()
    assert len(service.filter_by_product_type(InsuranceType.AUTO)) == len(service.list_markets())


def test_filter_by_distribution_type() -> None:
    service = MarketRegistryService()
    direct = service.filter_by_distribution_type(DistributionType.DIRECT)
    assert direct and all(e.distribution_type is DistributionType.DIRECT for e in direct)


def test_filter_by_product_scope() -> None:
    service = MarketRegistryService()
    collector = service.filter_by_product_scope(ProductScope.COLLECTOR)
    assert collector and all(e.product_scope is ProductScope.COLLECTOR for e in collector)


def test_find_by_distinct_rate_source_id() -> None:
    service = MarketRegistryService()
    # Unknown ids never match; the verified Square One source does.
    assert service.find_by_distinct_rate_source_id("anything") == []
    assert [
        e.registry_id
        for e in service.find_by_distinct_rate_source_id("RS-ZURICH-AUTO")
    ] == ["square-one"]


# --- distinct rate source / freshness ----------------------------------

def test_unverified_rate_source_not_guessed() -> None:
    """Routes that are NOT verified must not have an assigned rate source.

    Verified routes MAY carry an evidence-backed distinct_rate_source_id. The
    invariant is: an unverified route is never assigned a guessed source id.
    """
    service = MarketRegistryService()
    for entry in service.list_markets():
        if entry.status is not RegistryStatus.VERIFIED:
            assert entry.distinct_rate_source_id is None


def test_square_one_verified_rate_source() -> None:
    """Square One is the first verified distinct rate source (Zurich)."""
    service = MarketRegistryService()
    entry = service.get_by_registry_id("square-one")
    assert entry is not None
    assert entry.status is RegistryStatus.VERIFIED
    assert entry.distinct_rate_source_id == "RS-ZURICH-AUTO"
    assert entry.insurer_group == "Zurich"
    assert entry.last_verified_at is not None


def test_distinct_rate_source_verifiable() -> None:
    entry = MarketRegistryEntry.model_validate(_record(distinct_rate_source_id="rs-1"))
    assert entry.distinct_rate_source_id == "rs-1"


def test_verified_requires_timestamp() -> None:
    with pytest.raises(ValidationError):
        MarketRegistryEntry.model_validate(_record(status="verified"))


def test_freshness_helpers(tmp_path) -> None:
    verified = _record("v1", status="verified", last_verified_at="2026-08-09T12:00:00Z", distinct_rate_source_id="rs-1")
    discovered = _record("d1", status="discovered")
    directory = _write_registry(tmp_path, [verified, discovered])
    service = MarketRegistryService(registry_dir=directory)
    assert len(service.verified_records()) == 1
    assert len(service.records_missing_verification()) == 1
    assert service.freshness_percentage() == 50.0
    assert service.find_by_distinct_rate_source_id("rs-1")[0].registry_id == "v1"


def test_source_url_and_timestamp_serialization() -> None:
    entry = MarketRegistryEntry.model_validate(
        _record("url-1", status="verified", last_verified_at="2026-08-09T12:00:00Z",
                source_url="https://example.com/verify", quote_url="https://example.com/quote")
    )
    dumped = entry.model_dump(mode="json")
    assert dumped["last_verified_at"] == "2026-08-09T12:00:00Z"
    assert dumped["source_url"] == "https://example.com/verify"


# --- load failures -----------------------------------------------------

def test_invalid_record_fails_loading(tmp_path) -> None:
    directory = _write_registry(tmp_path, [_record("ok"), _record("bad", distribution_type="bogus")])
    with pytest.raises(RegistryLoadError):
        MarketRegistryService(registry_dir=directory)


def test_duplicate_registry_id_fails_loading(tmp_path) -> None:
    directory = _write_registry(tmp_path, [_record("dup"), _record("dup")])
    with pytest.raises(RegistryLoadError, match="duplicate"):
        MarketRegistryService(registry_dir=directory)


# --- dynamic-change regression (B15) -----------------------------------

def test_scenario_a_new_market_without_code_change(tmp_path) -> None:
    records = [_record("seed-1", brand_or_program="Existing Broker")]
    directory = _write_registry(tmp_path, records)
    service = MarketRegistryService(registry_dir=directory)
    assert len(service.list_markets()) == 1

    # Add a brand-new market to the DATA (no service code change).
    records.append(_record("brand-new-route", brand_or_program="New Insurer",
                            distribution_type="direct", product_scope="nonstandard_PPA",
                            requirements=["licence", "vin", "membership"]))
    _write_registry(tmp_path, records)
    service2 = MarketRegistryService(registry_dir=directory)
    assert len(service2.list_markets()) == 2
    new_one = service2.get_by_registry_id("brand-new-route")
    assert new_one.brand_or_program == "New Insurer"
    assert new_one.distribution_type is DistributionType.DIRECT


def test_scenario_c_changing_url_phone_requirement_via_data(tmp_path) -> None:
    records = [_record("route-1", quote_url="https://example.com/old",
                       public_phone_route="1-800-OLD", requirements=["licence"])]
    directory = _write_registry(tmp_path, records)
    service = MarketRegistryService(registry_dir=directory)
    assert service.get_by_registry_id("route-1").quote_url == "https://example.com/old"

    records[0]["quote_url"] = "https://example.com/new"
    records[0]["public_phone_route"] = "1-800-NEW"
    records[0]["requirements"] = ["licence", "vin", "callback"]
    _write_registry(tmp_path, records)
    service2 = MarketRegistryService(registry_dir=directory)
    updated = service2.get_by_registry_id("route-1")
    assert updated.quote_url == "https://example.com/new"
    assert updated.public_phone_route == "1-800-NEW"
    assert [r.value for r in updated.requirements] == ["callback", "licence", "vin"]


# --- privacy / serialization -------------------------------------------

def test_registry_contains_no_applicant_pii() -> None:
    service = MarketRegistryService()
    payload = json.dumps([e.model_dump(mode="json") for e in service.list_markets()])
    for forbidden in ("test.applicant@", "T0000-00000-00000", "1HGCM82633A000000",
                      "416-555", "M0A 0A0", "1990-01-01"):
        assert forbidden not in payload


def test_registry_serialization_for_hackathon_submission() -> None:
    service = MarketRegistryService()
    payload = json.dumps([e.model_dump(mode="json") for e in service.list_markets()], indent=2)
    assert payload.startswith("[")
    assert '"registry_id"' in payload
    # Enums serialize to their string values.
    assert '"distribution_type": "direct"' in payload


def test_registry_status_distinct_from_quote_terminal_statuses() -> None:
    registry_statuses = {s.value for s in RegistryStatus}
    assert registry_statuses.isdisjoint(QUOTE_TERMINAL_STATUSES)
