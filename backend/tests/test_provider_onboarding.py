"""Provider onboarding utility - focused hermetic tests.

Covers the deterministic, SAFE parts only: canonical mapping localization,
URL/host validation, draft generation (never auto-verified), human-gated
promotion, demo-routes-cannot-become-live, and aggregator compatibility.

NO real provider traffic: everything runs against temp dirs and synthetic
registry/draft data. Ordinary pytest stays hermetic.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.models.registry import MarketRegistryEntry, RegistryStatus
from app.services.onboarding import (
    OnboardingError,
    build_draft_config,
    build_report,
    derive_allowed_hosts,
    load_draft,
    map_label,
    map_labels,
    mark_registry_verified,
    promote_draft,
    save_draft,
    validate_candidate_url,
)


def _entry(registry_id: str = "acme-direct", status: RegistryStatus = RegistryStatus.DISCOVERED) -> MarketRegistryEntry:
    return MarketRegistryEntry(
        registry_id=registry_id,
        brand_or_program="Acme Insurance",
        distribution_type="direct",
        status=status,
    )


def _write_registry(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "registry" / "auto.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_comment": "test registry", "records": list(records)}, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# URL / host validation
# ---------------------------------------------------------------------------


def test_candidate_url_must_be_https_and_have_host():
    assert validate_candidate_url("https://www.example.ca/auto/") == "https://www.example.ca/auto/"
    with pytest.raises(OnboardingError):
        validate_candidate_url("http://www.example.ca/auto/")
    with pytest.raises(OnboardingError):
        validate_candidate_url("https:///no-host")
    with pytest.raises(OnboardingError):
        validate_candidate_url("")


def test_derive_allowed_hosts_uses_final_host():
    assert derive_allowed_hosts("https://secure.example.ca/auto#province", "x") == ["secure.example.ca"]


# ---------------------------------------------------------------------------
# Deterministic canonical mapping (localization, no guessing)
# ---------------------------------------------------------------------------


def test_canonical_mapping_localization():
    mapping = map_label("Postal code")
    assert mapping.mapped and mapping.canonical_path == "applicant.address.postal_code"

    mapping2 = map_label("Approximate annual distance driven")
    assert mapping2.mapped and mapping2.canonical_path == "product_data.vehicles[0].use.annual_kilometres"

    mapping3 = map_label("Number of vehicles")
    assert mapping3.mapped and mapping3.collection_length is True
    assert mapping3.canonical_path == "product_data.vehicles"

    mapping4 = map_label("Date of birth")
    assert mapping4.mapped and mapping4.canonical_path == "applicant.identity.date_of_birth"


def test_canonical_mapping_never_guesses():
    mapping = map_label("What is your favourite colour?")
    assert not mapping.mapped
    assert mapping.reason == "unmapped_field"
    assert mapping.canonical_path is None


def test_map_labels_splits_mapped_and_unmapped():
    mapped, unmapped = map_labels([
        ("Postal code", "input"),
        ("Legal name", "input"),
        ("Some mystery field", "input"),
    ])
    assert {m.canonical_path for m in mapped} == {
        "applicant.address.postal_code", "applicant.identity.legal_name",
    }
    assert [u.reason for u in unmapped] == ["unmapped_field"]


# ---------------------------------------------------------------------------
# Draft can never become verified automatically
# ---------------------------------------------------------------------------


def test_draft_never_verified_automatically(tmp_path):
    entry = _entry()
    drafts = tmp_path / "drafts"
    live = tmp_path / "live"
    registry_path = _write_registry(tmp_path, entry.model_dump(mode="json"))

    draft = build_draft_config(
        entry,
        start_url="https://www.example.ca/auto",
        allowed_hosts=["www.example.ca"],
        heading="Car Insurance",
        url_pattern="www.example.ca/auto",
        mapped_fields=[map_label("Postal code")],
        observed_buttons=["Continue"],
        access_control_detected=False,
        callback_detected=False,
    )
    assert draft.last_verified_at is None  # never auto-verified

    save_draft(drafts, entry.registry_id, draft, {})
    # Registry is untouched.
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert data["records"][0]["status"] == RegistryStatus.DISCOVERED.value
    assert data["records"][0]["last_verified_at"] is None
    # Live dir has nothing.
    assert not (live / f"{entry.registry_id}.json").exists()
    # Draft file notes it is not verified.
    loaded = load_draft(drafts, entry.registry_id)
    assert loaded is not None and loaded.last_verified_at is None
    assert "NOT verified" in (loaded.automation_notes or "")


# ---------------------------------------------------------------------------
# Draft -> human-approved promotion
# ---------------------------------------------------------------------------


def _prepare_draft(tmp_path: Path, registry_id: str = "acme-direct"):
    entry = _entry(registry_id)
    registry_path = _write_registry(tmp_path, entry.model_dump(mode="json"))
    drafts = tmp_path / "drafts"
    live = tmp_path / "live"
    draft = build_draft_config(
        entry,
        start_url="https://www.example.ca/auto",
        allowed_hosts=["www.example.ca"],
        heading="Car Insurance",
        url_pattern="www.example.ca/auto",
        mapped_fields=[map_label("Postal code"), map_label("Annual kilometres")],
        observed_buttons=["Continue"],
        access_control_detected=False,
        callback_detected=False,
    )
    save_draft(drafts, registry_id, draft, {})
    return entry, drafts, live, registry_path


def test_promote_requires_confirmation(tmp_path):
    entry, drafts, live, registry_path = _prepare_draft(tmp_path)
    with pytest.raises(OnboardingError, match="confirmation"):
        promote_draft(
            drafts_dir=drafts, live_dir=live, registry_path=registry_path,
            registry_id=entry.registry_id, confirmed=False,
        )


def test_promote_requires_existing_draft(tmp_path):
    entry = _entry()
    with pytest.raises(OnboardingError, match="no draft"):
        promote_draft(
            drafts_dir=tmp_path / "drafts", live_dir=tmp_path / "live",
            registry_path=_write_registry(tmp_path, entry.model_dump(mode="json")),
            registry_id=entry.registry_id, confirmed=True,
        )


def test_promote_writes_live_config_and_marks_verified(tmp_path):
    entry, drafts, live, registry_path = _prepare_draft(tmp_path)
    result = promote_draft(
        drafts_dir=drafts, live_dir=live, registry_path=registry_path,
        registry_id=entry.registry_id, confirmed=True,
        source_url="https://www.example.ca/auto",
        evidence_artifact="test-approval",
    )
    assert result["live_config_path"].endswith("acme-direct.json")
    live_cfg = json.loads((live / "acme-direct.json").read_text(encoding="utf-8"))
    assert live_cfg["last_verified_at"] is not None
    assert live_cfg["start_url"] == "https://www.example.ca/auto"

    data = json.loads(registry_path.read_text(encoding="utf-8"))
    rec = data["records"][0]
    assert rec["status"] == RegistryStatus.VERIFIED.value
    assert rec["last_verified_at"] is not None
    assert rec["source_url"] == "https://www.example.ca/auto"
    # A verified record must carry a timestamp (model invariant).
    MarketRegistryEntry.model_validate(rec)


def test_mark_registry_verified_rejects_unknown_id(tmp_path):
    entry = _entry("acme-direct")
    registry_path = _write_registry(tmp_path, entry.model_dump(mode="json"))
    with pytest.raises(OnboardingError, match="not found"):
        mark_registry_verified(
            registry_path=registry_path, registry_id="ghost",
            verified_at=dt.datetime.now(dt.timezone.utc),
        )


# ---------------------------------------------------------------------------
# Soft bot-block classification (generic regression, no bypass)
# ---------------------------------------------------------------------------


def test_soft_bot_block_text_classification():
    from app.services.onboarding.inspection import is_bot_block_text

    assert is_bot_block_text("Sorry, you have been blocked") is True
    assert is_bot_block_text("We use Cloudflare to protect this site") is True
    assert is_bot_block_text("Access denied") is True
    assert is_bot_block_text("Get a quote", "Enter your postal code") is False
    assert is_bot_block_text("Your Quote", "Annual premium $1,200") is False


# ---------------------------------------------------------------------------
# Demo routes cannot become live (they are not in the real registry)
# ---------------------------------------------------------------------------


def test_demo_registry_id_refused_by_onboarding(tmp_path):
    from app.tools.provider_onboarding import _registry_entry
    from app.services.onboarding.repository import default_registry_path

    with pytest.raises(OnboardingError, match="not found in the Market Registry"):
        _registry_entry(default_registry_path(), "mock-insurer")


# ---------------------------------------------------------------------------
# Aggregator multi-result compatibility
# ---------------------------------------------------------------------------


def test_aggregator_draft_is_compatible_and_never_verified(tmp_path):
    entry = MarketRegistryEntry(
        registry_id="acme-aggregator",
        brand_or_program="Acme Compare",
        distribution_type="aggregator",
        status=RegistryStatus.DISCOVERED,
    )
    draft = build_draft_config(
        entry,
        start_url="https://www.acmecompare.ca/auto",
        allowed_hosts=["www.acmecompare.ca"],
        heading="Compare Auto Quotes",
        url_pattern="www.acmecompare.ca/auto",
        mapped_fields=[map_label("Postal code")],
        observed_buttons=["Compare rates"],
        access_control_detected=False,
        callback_detected=False,
        aggregator=True,
    )
    # Draft validates (usable by the multi-observation pipeline) + not verified.
    assert draft.registry_id == "acme-aggregator"
    assert draft.last_verified_at is None

    report = build_report(
        entry,
        start_url="https://www.acmecompare.ca/auto",
        final_url="https://www.acmecompare.ca/auto",
        allowed_hosts=["www.acmecompare.ca"],
        heading="Compare Auto Quotes",
        page_signature_ids=[],
        mapped_fields=[map_label("Postal code")],
        unmapped_fields=[],
        observed_buttons=["Compare rates"],
        privacy_banner_detected=True,
        access_control_detected=False,
        callback_detected=False,
        quote_detected=True,
        draft_path="drafts/acme-aggregator.json",
        safe_to_live_test=False,
        aggregator=True,
    )
    assert report["aggregator_route"] is True
    assert report["verified"] is False
    # The tool never auto-approves an aggregator either.
    assert report["safe_to_live_test"] is False


# ---------------------------------------------------------------------------
# Unknown/new fields are reported, not guessed
# ---------------------------------------------------------------------------


def test_new_fields_reported_as_canonical_field_missing(tmp_path):
    entry = _entry()
    from app.services.onboarding.canonical import map_label
    from app.services.onboarding.draft import mark_unmapped_fields

    mystery = map_label("Loyalty membership number")
    missing, proposed = mark_unmapped_fields([mystery])
    assert missing == ["Loyalty membership number"]
    assert proposed and proposed[0]["observed_label"] == "Loyalty membership number"
