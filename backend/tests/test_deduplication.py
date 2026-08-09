"""Tests for Issue #4: rate-source deduplication (core + dynamic changes).

All tests use synthetic data in temporary directories. Hermetic: no network,
no LangSmith, no LLM.
"""

from __future__ import annotations

import json
from typing import Optional

import pytest
from pydantic import ValidationError

from app.models.dedup import (
    Confidence,
    DeduplicationStatus,
    DistinctRateSource,
    ReasonCode,
)
from app.models.registry import MarketRegistryEntry
from app.services.deduplication import (
    DedupLoadError,
    DedupLookupError,
    RateSourceDeduplicationService,
)
from app.services.market_registry import MarketRegistryService


def _entry(registry_id: str, **overrides) -> dict:
    base = {
        "registry_id": registry_id,
        "product_type": "auto",
        "brand_or_program": registry_id,
        "distribution_type": "direct",
        "product_scope": "standard_PPA",
        "status": "discovered",
    }
    base.update(overrides)
    return base


def _rate_source(rate_source_id: str, **overrides) -> dict:
    base = {
        "distinct_rate_source_id": rate_source_id,
        "product_type": "auto",
        "insurer_group": "TEST-GROUP",
        "related_registry_ids": [],
        "deduplication_status": "unique",
        "confidence": "high",
    }
    base.update(overrides)
    return base


def _make_service(tmp_path, entries, rate_sources):
    reg_dir = tmp_path / "reg"
    rs_dir = tmp_path / "rs"
    reg_dir.mkdir(parents=True, exist_ok=True)
    rs_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "auto.json").write_text(json.dumps({"records": entries}), encoding="utf-8")
    (rs_dir / "auto_rate_sources.json").write_text(json.dumps({"records": rate_sources}), encoding="utf-8")
    registry = MarketRegistryService(registry_dir=reg_dir)
    return RateSourceDeduplicationService(registry_service=registry, rate_sources_dir=rs_dir)


# --- 1/2/3. unique + confirmed duplicates -----------------------------

def test_single_route_unique_rate_source(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", distinct_rate_source_id="RS-1")],
        [_rate_source("RS-1", related_registry_ids=["r1"])],
    )
    assert service.count_distinct_rate_sources() == 1
    assert service.get_duplicate_groups() == []
    view = service.deduplicated_registry_view()
    assert view[0].deduplication_status is DeduplicationStatus.UNIQUE
    assert view[0].registry_id == "r1"


def test_two_routes_same_confirmed_source(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", distinct_rate_source_id="RS-1"), _entry("r2", distinct_rate_source_id="RS-1")],
        [_rate_source("RS-1", related_registry_ids=["r1", "r2"])],
    )
    assert service.count_distinct_rate_sources() == 1
    groups = service.get_duplicate_groups()
    assert len(groups) == 1 and set(groups[0].registry_ids) == {"r1", "r2"}
    metrics = service.metrics()
    assert metrics["confirmed_rate_sources"] == 1
    assert metrics["confirmed_duplicates"] == 1


def test_three_routes_one_source(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [
            _entry("r1", distinct_rate_source_id="RS-1"),
            _entry("r2", distinct_rate_source_id="RS-1"),
            _entry("r3", distinct_rate_source_id="RS-1"),
        ],
        [_rate_source("RS-1", related_registry_ids=["r1", "r2", "r3"])],
    )
    groups = service.get_duplicate_groups()
    assert len(groups) == 1 and len(groups[0].registry_ids) == 3
    assert service.metrics()["confirmed_duplicates"] == 2


# --- 4/5/6/7. same group / underwriter / explicit id / null ------------

def test_same_group_not_automatically_deduplicated(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", insurer_group="GRP-X"), _entry("r2", insurer_group="GRP-X")],
        [],
    )
    decision = service.evaluate_pair("r1", "r2")
    assert decision.decision is DeduplicationStatus.DUPLICATE_POSSIBLE
    assert decision.reason_code is ReasonCode.SAME_GROUP_ONLY_INSUFFICIENT
    assert service.count_distinct_rate_sources() == 0  # not collapsed
    assert len(service.get_unresolved_mappings()) == 2


def test_same_underwriter_is_candidate_not_confirmed(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", legal_underwriter="Underwriter Y"), _entry("r2", legal_underwriter="Underwriter Y")],
        [],
    )
    decision = service.evaluate_pair("r1", "r2")
    assert decision.decision is DeduplicationStatus.DUPLICATE_POSSIBLE
    assert decision.reason_code is ReasonCode.SAME_UNDERWRITER_POSSIBLE_DUPLICATE
    # Candidate surfaced but NOT confirmed.
    assert service.metrics()["confirmed_duplicates"] == 0


def test_explicit_same_id_confirmed(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", distinct_rate_source_id="RS-1"), _entry("r2", distinct_rate_source_id="RS-1")],
        [],
    )
    decision = service.evaluate_pair("r1", "r2")
    assert decision.decision is DeduplicationStatus.DUPLICATE_CONFIRMED
    assert decision.reason_code is ReasonCode.SAME_VERIFIED_RATE_SOURCE
    assert decision.distinct_rate_source_id == "RS-1"


def test_null_mapping_stays_unresolved(tmp_path) -> None:
    service = _make_service(tmp_path, [_entry("r1")], [])
    assert service.get_unresolved_mappings()[0].registry_id == "r1"
    assert service.metrics()["unresolved_mappings"] == 1


# --- 8/9/10/11/12. candidates / counts / views -------------------------

def test_duplicate_candidates_surfaced(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", insurer_group="GRP-X"), _entry("r2", insurer_group="GRP-X"), _entry("r3")],
        [],
    )
    candidates = service.find_duplicate_candidates("r1")
    assert [c.registry_id for c in candidates] == ["r2"]
    assert candidates[0].reason_code is ReasonCode.SAME_GROUP_ONLY_INSUFFICIENT


def test_confirmed_count_once_and_view(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [
            _entry("r1", distinct_rate_source_id="RS-1"),
            _entry("r2", distinct_rate_source_id="RS-1"),
            _entry("r3"),
        ],
        [],
    )
    view = service.deduplicated_registry_view()
    # r1+r2 collapse to one row; r3 stays visible.
    assert len(view) == 2
    rows = {v.registry_id: v for v in view}
    assert rows["r1"].deduplication_status is DeduplicationStatus.DUPLICATE_CONFIRMED
    assert rows["r1"].group_members == ["r1", "r2"]
    assert rows["r3"].deduplication_status is DeduplicationStatus.UNRESOLVED
    assert service.metrics()["confirmed_duplicates"] == 1


def test_possible_duplicates_not_suppressed(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", insurer_group="GRP-X"), _entry("r2", insurer_group="GRP-X")],
        [],
    )
    view = service.deduplicated_registry_view()
    assert len(view) == 2  # both remain visible
    assert all(v.deduplication_status is DeduplicationStatus.UNRESOLVED for v in view)
    assert service.metrics()["confirmed_duplicates"] == 0


def test_original_registry_records_preserved(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [
            _entry("r1", distinct_rate_source_id="RS-1"),
            _entry("r2", distinct_rate_source_id="RS-1"),
        ],
        [],
    )
    entries = service._registry.list_markets()  # original registry untouched
    assert len(entries) == 2
    assert {e.registry_id for e in entries} == {"r1", "r2"}


# --- 13/14/15. explanation / serialization -----------------------------

def test_decision_includes_explanation(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", distinct_rate_source_id="RS-1"), _entry("r2", distinct_rate_source_id="RS-1")],
        [],
    )
    decision = service.evaluate_pair("r1", "r2")
    assert decision.evidence
    assert decision.reason_code is ReasonCode.SAME_VERIFIED_RATE_SOURCE
    assert decision.distinct_rate_source_id == "RS-1"
    assert decision.confidence is Confidence.HIGH


def test_confidence_and_reason_serialization(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", insurer_group="GRP-X"), _entry("r2", insurer_group="GRP-X")],
        [],
    )
    decision = service.evaluate_pair("r1", "r2")
    dumped = decision.model_dump(mode="json")
    assert dumped["confidence"] == "low"
    assert dumped["reason_code"] == "same_group_only_insufficient"


# --- 16/17. unknown ids fail cleanly ------------------------------------

def test_unknown_registry_id_fails_cleanly(tmp_path) -> None:
    service = _make_service(tmp_path, [_entry("r1")], [])
    with pytest.raises(DedupLookupError):
        service.evaluate_pair("r1", "nope")
    with pytest.raises(DedupLookupError):
        service.find_duplicate_candidates("nope")


def test_unknown_rate_source_id_fails_cleanly(tmp_path) -> None:
    service = _make_service(tmp_path, [_entry("r1")], [])
    assert service.get_rate_source("nope") is None
    assert service.get_registry_entries_for_rate_source("nope") == []


# --- 18/19. mapping validation ------------------------------------------

def test_rate_source_data_validates(tmp_path) -> None:
    with pytest.raises(ValidationError):
        DistinctRateSource.model_validate(_rate_source("RS-1", confidence="bogus"))
    with pytest.raises(ValidationError):
        DistinctRateSource.model_validate(_rate_source("", ))


def test_duplicate_rate_source_ids_fail_loading(tmp_path) -> None:
    reg_dir = _write_reg(tmp_path, [_entry("r1", distinct_rate_source_id="RS-1")])
    rs_dir = tmp_path / "rs"
    rs_dir.mkdir()
    (rs_dir / "auto_rate_sources.json").write_text(
        json.dumps({"records": [_rate_source("RS-1"), _rate_source("RS-1")]}), encoding="utf-8"
    )
    registry = MarketRegistryService(registry_dir=reg_dir)
    with pytest.raises(DedupLoadError, match="duplicate"):
        RateSourceDeduplicationService(registry_service=registry, rate_sources_dir=rs_dir)


def test_related_registry_ids_cannot_contradict_registry(tmp_path) -> None:
    registry = MarketRegistryService(registry_dir=_write_reg(tmp_path, [_entry("r1", distinct_rate_source_id="RS-1")]))
    rs_dir = tmp_path / "rs"
    rs_dir.mkdir()
    # Claims r1 maps to RS-9, but the registry says RS-1 -> must fail.
    (rs_dir / "auto_rate_sources.json").write_text(
        json.dumps({"records": [_rate_source("RS-9", related_registry_ids=["r1"])]}), encoding="utf-8"
    )
    with pytest.raises(DedupLoadError, match="inconsistent"):
        RateSourceDeduplicationService(registry_service=registry, rate_sources_dir=rs_dir)


def test_related_registry_ids_must_exist(tmp_path) -> None:
    registry = MarketRegistryService(registry_dir=_write_reg(tmp_path, [_entry("r1")]))
    rs_dir = tmp_path / "rs"
    rs_dir.mkdir()
    (rs_dir / "auto_rate_sources.json").write_text(
        json.dumps({"records": [_rate_source("RS-1", related_registry_ids=["missing-route"])]}),
        encoding="utf-8",
    )
    with pytest.raises(DedupLoadError, match="not found"):
        RateSourceDeduplicationService(registry_service=registry, rate_sources_dir=rs_dir)


def _write_reg(tmp_path, entries):
    reg_dir = tmp_path / "reg"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "auto.json").write_text(json.dumps({"records": entries}), encoding="utf-8")
    return reg_dir


# --- 20. existing registry loading still works --------------------------

def test_existing_registry_loading_still_works(tmp_path) -> None:
    registry = MarketRegistryService()
    assert len(registry.list_markets()) > 0


# --- Dynamic-change scenarios (section 20) ------------------------------

def test_scenario_a_new_route_auto_groups(tmp_path) -> None:
    records = [_entry("broker-1", distinct_rate_source_id="RS-TEST-001")]
    reg_dir = _write_reg(tmp_path, records)
    rs_dir = tmp_path / "rs"
    rs_dir.mkdir()
    (rs_dir / "auto_rate_sources.json").write_text(
        json.dumps({"records": [_rate_source("RS-TEST-001", related_registry_ids=["broker-1"])]}),
        encoding="utf-8",
    )
    registry = MarketRegistryService(registry_dir=reg_dir)
    service = RateSourceDeduplicationService(registry_service=registry, rate_sources_dir=rs_dir)
    assert service.count_distinct_rate_sources() == 1

    # Add a NEW route to RS-TEST-001 via DATA only.
    records.append(_entry("broker-2", distinct_rate_source_id="RS-TEST-001"))
    _write_reg(tmp_path, records)
    registry2 = MarketRegistryService(registry_dir=reg_dir)
    service2 = RateSourceDeduplicationService(registry_service=registry2, rate_sources_dir=rs_dir)
    groups = service2.get_duplicate_groups()
    assert len(groups) == 1 and set(groups[0].registry_ids) == {"broker-1", "broker-2"}


def test_scenario_b_remap_via_data(tmp_path) -> None:
    # Grouping is derived from the authoritative registry mapping; the rate
    # sources file stays empty so a remap is a single data change.
    records = [
        _entry("broker-1", distinct_rate_source_id="RS-TEST-001"),
        _entry("broker-2", distinct_rate_source_id="RS-TEST-001"),
    ]
    reg_dir = _write_reg(tmp_path, records)
    rs_dir = tmp_path / "rs"
    rs_dir.mkdir()
    (rs_dir / "auto_rate_sources.json").write_text(json.dumps({"records": []}), encoding="utf-8")
    registry = MarketRegistryService(registry_dir=reg_dir)
    service = RateSourceDeduplicationService(registry_service=registry, rate_sources_dir=rs_dir)
    assert service.count_distinct_rate_sources() == 1
    assert service.metrics()["confirmed_duplicates"] == 1

    # Remap broker-2 to a DIFFERENT verified source via registry data only.
    records[1]["distinct_rate_source_id"] = "RS-TEST-002"
    _write_reg(tmp_path, records)
    registry2 = MarketRegistryService(registry_dir=reg_dir)
    service2 = RateSourceDeduplicationService(registry_service=registry2, rate_sources_dir=rs_dir)
    assert service2.count_distinct_rate_sources() == 2
    assert service2.metrics()["confirmed_duplicates"] == 0
    decision = service2.evaluate_pair("broker-1", "broker-2")
    assert decision.decision is DeduplicationStatus.UNIQUE
    assert decision.reason_code is ReasonCode.EXPLICITLY_DISTINCT_PROGRAM


def test_scenario_c_same_group_distinct_programs_remain_separate(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [
            _entry("r1", insurer_group="GRP", distinct_rate_source_id="RS-A"),
            _entry("r2", insurer_group="GRP", distinct_rate_source_id="RS-B"),
        ],
        [],
    )
    decision = service.evaluate_pair("r1", "r2")
    assert decision.decision is DeduplicationStatus.UNIQUE
    assert service.metrics()["confirmed_duplicates"] == 0
    assert service.count_distinct_rate_sources() == 2


def test_scenario_d_unknown_relationship_not_collapsed(tmp_path) -> None:
    service = _make_service(
        tmp_path,
        [_entry("r1", insurer_group="GRP"), _entry("r2", insurer_group="GRP")],
        [],
    )
    assert service.metrics()["confirmed_rate_sources"] == 0
    assert service.metrics()["unresolved_mappings"] == 2
    assert service.evaluate_pair("r1", "r2").decision is DeduplicationStatus.DUPLICATE_POSSIBLE


def test_scenario_e_new_field_does_not_break_dedup(tmp_path) -> None:
    """Dedup reads only the fields it needs; extra fields are irrelevant."""

    class EntryWithExtra(MarketRegistryEntry):
        irrelevant_metadata: Optional[str] = None

    entries = [
        EntryWithExtra.model_validate(_entry("r1", distinct_rate_source_id="RS-1", irrelevant_metadata="extra-a")),
        EntryWithExtra.model_validate(_entry("r2", distinct_rate_source_id="RS-1", irrelevant_metadata="extra-b")),
    ]

    class StubRegistry:
        def __init__(self, records):
            self._records = records

        def list_markets(self):
            return self._records

        def get_by_registry_id(self, registry_id):
            return next((e for e in self._records if e.registry_id == registry_id), None)

    rs_dir = tmp_path / "rs"
    rs_dir.mkdir()
    (rs_dir / "auto_rate_sources.json").write_text(json.dumps({"records": []}), encoding="utf-8")
    service = RateSourceDeduplicationService(registry_service=StubRegistry(entries), rate_sources_dir=rs_dir)
    assert service.evaluate_pair("r1", "r2").decision is DeduplicationStatus.DUPLICATE_CONFIRMED
    assert service.metrics()["confirmed_duplicates"] == 1
