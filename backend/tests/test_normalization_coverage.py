"""Coverage mapping registry + CoverageNormalizer tests (Issue #11)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.normalization import (
    CoverageItemKey,
    CoverageItemState,
    CoverageProvenance,
)
from app.services.normalization.config import (
    CoverageMappingRegistry,
    NormalizationConfigError,
    default_normalization_data_dir,
    _normalize_label,
)
from app.services.normalization.coverage import CoverageNormalizer, _parse_amount

REGISTRY = CoverageMappingRegistry()


def test_registry_loads_real_data_file():
    assert REGISTRY.rule_version == "1"
    assert REGISTRY.rule_count >= 10
    assert REGISTRY.currency == "CAD"


def test_default_dir_points_at_data_folder():
    path = default_normalization_data_dir()
    assert path.name == "normalization"
    assert (path / "auto_coverage_mappings.json").exists()


def test_resolve_exact_alias():
    rule = REGISTRY.resolve("Third Party Liability")
    assert rule is not None
    assert rule.canonical_key == CoverageItemKey.THIRD_PARTY_LIABILITY


def test_resolve_phrase_prefix():
    rule = REGISTRY.resolve("Third Party Liability - $2,000,000")
    assert rule is not None
    assert rule.canonical_key == CoverageItemKey.THIRD_PARTY_LIABILITY


def test_resolve_longest_alias_wins():
    # "liability" and "liability coverage" both -> TPL; longest alias used.
    rule = REGISTRY.resolve("liability coverage")
    assert rule is not None
    assert rule.canonical_key == CoverageItemKey.THIRD_PARTY_LIABILITY


def test_resolve_unknown_returns_none():
    assert REGISTRY.resolve("Some Exotic Coverage Nobody Knows") is None


def test_normalize_label_is_deterministic():
    assert _normalize_label("Third-Party Liability") == "third party liability"
    assert _normalize_label("THIRD PARTY LIABILITY") == "third party liability"


def test_registry_rejects_duplicate_aliases(tmp_path):
    (tmp_path / "auto_coverage_mappings.json").write_text(
        """
        {
          "rule_version": "1",
          "currency": "CAD",
          "annualization": {},
          "value_parsers": {},
          "coverage_mappings": [
            {"canonical_key": "third_party_liability", "aliases": ["liability"], "value_type": "limit", "default_state": "included"},
            {"canonical_key": "collision", "aliases": ["liability"], "value_type": "deductible", "default_state": "included"}
          ]
        }
        """,
        encoding="utf-8",
    )
    with pytest.raises(NormalizationConfigError):
        CoverageMappingRegistry(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# CoverageNormalizer
# ---------------------------------------------------------------------------


def _normalizer() -> CoverageNormalizer:
    return CoverageNormalizer(REGISTRY)


def test_maps_limits_and_deductibles():
    ledger = _normalizer().normalize(
        coverage_observations=[
            "Third Party Liability - $2,000,000",
            "Collision - $1,000 deductible",
        ]
    )
    tpl = ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY)
    assert tpl is not None
    assert tpl.state == CoverageItemState.INCLUDED
    assert tpl.value.amount == Decimal("2000000")
    assert tpl.value.currency == "CAD"
    assert tpl.provenance == CoverageProvenance.MAPPED_ALIAS

    coll = ledger.get(CoverageItemKey.COLLISION)
    assert coll is not None
    assert coll.value.amount == Decimal("1000")
    assert coll.raw_labels == ["Collision - $1,000 deductible"]


def test_maps_discounts():
    ledger = _normalizer().normalize(discount_observations=["Winter Tire Discount"])
    winter = ledger.get(CoverageItemKey.WINTER_TIRES_DISCOUNT)
    assert winter is not None
    assert winter.state == CoverageItemState.INCLUDED
    assert winter.value.present is True


def test_unmapped_labels_preserved_not_guessed():
    ledger = _normalizer().normalize(
        coverage_observations=["Mystery Endorsement - $5", "Third Party Liability - $1M"]
    )
    assert len(ledger.unmapped_coverage) == 1
    assert ledger.unmapped_coverage[0].provider_label == "Mystery Endorsement - $5"
    # The mapped item is still present
    assert ledger.get(CoverageItemKey.THIRD_PARTY_LIABILITY) is not None
    assert ledger.mapped_count == 1


def test_unknown_never_becomes_excluded():
    ledger = _normalizer().normalize(coverage_observations=[])
    # Empty ledger has zero items (unknown is implied), and nothing is excluded.
    assert ledger.mapped_count == 0
    assert not any(
        item.state == CoverageItemState.EXCLUDED for item in ledger.ordered_items()
    )


def test_explicit_excluded_label_sets_excluded():
    ledger = _normalizer().normalize(coverage_observations=["Family Protection: Excluded"])
    fp = ledger.get(CoverageItemKey.OPCF_44R_FAMILY_PROTECTION)
    assert fp is not None
    assert fp.state == CoverageItemState.EXCLUDED
    assert fp.value.code == "OPCF 44R"


def test_endorsement_code_is_concise():
    ledger = _normalizer().normalize(coverage_observations=["Family Protection"])
    fp = ledger.get(CoverageItemKey.OPCF_44R_FAMILY_PROTECTION)
    assert fp.value.code == "OPCF 44R"


def test_source_evidence_ids_attached():
    ledger = _normalizer().normalize(
        coverage_observations=["Collision - $500 deductible"],
        source_evidence_ids=["ev-1", "ev-2"],
    )
    coll = ledger.get(CoverageItemKey.COLLISION)
    assert coll.source_evidence_ids == ["ev-1", "ev-2"]


def test_duplicate_labels_merge_raw_labels():
    ledger = _normalizer().normalize(
        coverage_observations=["Collision - $500", "Collision - $750"]
    )
    coll = ledger.get(CoverageItemKey.COLLISION)
    assert coll.raw_labels == ["Collision - $500", "Collision - $750"]
    # First value retained when a later label carries no clearer value
    assert coll.value.amount == Decimal("500")


# ---------------------------------------------------------------------------
# amount parsing
# ---------------------------------------------------------------------------


def test_parse_amount_money_prefix():
    assert _parse_amount("$1,000,000") == Decimal("1000000")
    assert _parse_amount("$500.25") == Decimal("500.25")


def test_parse_amount_million_suffix():
    assert _parse_amount("2m") == Decimal("2000000")
    assert _parse_amount("1 million") == Decimal("1000000")


def test_parse_amount_plain_number():
    assert _parse_amount("$0 deductible") == Decimal("0")
    assert _parse_amount("Collision - $750 deductible") == Decimal("750")


def test_parse_amount_none_when_no_amount():
    assert _parse_amount("Family Protection") is None
