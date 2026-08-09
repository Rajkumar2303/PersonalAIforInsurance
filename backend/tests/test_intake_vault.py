"""Tests for the profile vault (Issue #5, section 32).

Covers in-memory and encrypted-at-rest implementations: create/get/update/
delete, no accidental overwrite, no plaintext PII in persisted files, key from
config only, and temporary test storage.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models.insurance import (
    AddressInformation,
    ApplicantIdentity,
    ApplicantInformation,
    AutoInsuranceProfile,
    ConsentState,
    ContactInformation,
    InsuranceProfile,
    InsuranceType,
    Province,
    QuoteMode,
)
from app.services.intake.vault import (
    EncryptedFileProfileVault,
    InMemoryProfileVault,
    ProfileNotFoundError,
    generate_vault_key,
    vault_key_from_secret,
)

from intake_helpers import SYNTHETIC_LICENCE, SYNTHETIC_POSTAL, SYNTHETIC_VIN


def _profile() -> InsuranceProfile:
    return InsuranceProfile(
        insurance_type=InsuranceType.AUTO,
        consent=ConsentState(consent_timestamp=dt.datetime.now(dt.timezone.utc), quote_mode=QuoteMode.LIVE_QUOTE),
        applicant=ApplicantInformation(
            identity=ApplicantIdentity(legal_name="Test Applicant", date_of_birth=dt.date(1990, 1, 1)),
            contact=ContactInformation(),
            address=AddressInformation(province=Province.ON, postal_code=SYNTHETIC_POSTAL),
        ),
        product_data=AutoInsuranceProfile(),
    )


# --- in-memory vault ---------------------------------------------------

def test_memory_create_and_get() -> None:
    vault = InMemoryProfileVault()
    profile_id = vault.create(_profile())
    assert vault.get(profile_id) is not None
    assert vault.get(profile_id).applicant.identity.legal_name == "Test Applicant"


def test_memory_validated_update() -> None:
    vault = InMemoryProfileVault()
    profile_id = vault.create(_profile())
    updated = _profile().updated("applicant.identity.legal_name", "Updated Name")
    vault.update(profile_id, updated)
    assert vault.get(profile_id).applicant.identity.legal_name == "Updated Name"


def test_memory_delete() -> None:
    vault = InMemoryProfileVault()
    profile_id = vault.create(_profile())
    vault.delete(profile_id)
    assert vault.get(profile_id) is None
    assert vault.exists(profile_id) is False


def test_memory_update_missing_raises() -> None:
    vault = InMemoryProfileVault()
    with pytest.raises(ProfileNotFoundError):
        vault.update("nope", _profile())


def test_one_profile_cannot_overwrite_another() -> None:
    vault = InMemoryProfileVault()
    first_id = vault.create(_profile())
    second_id = vault.create(_profile())
    vault.update(second_id, _profile().updated("applicant.identity.legal_name", "Second"))
    assert vault.get(first_id).applicant.identity.legal_name == "Test Applicant"
    assert vault.get(second_id).applicant.identity.legal_name == "Second"


# --- encrypted-at-rest vault -------------------------------------------

def test_encrypted_roundtrip(tmp_path) -> None:
    key = generate_vault_key()
    vault = EncryptedFileProfileVault(key, tmp_path / "vault")
    profile_id = vault.create(_profile())
    loaded = vault.get(profile_id)
    assert loaded is not None
    assert loaded.applicant.identity.legal_name == "Test Applicant"
    assert loaded.applicant.address.postal_code == "M0A 0A0"


def test_encrypted_delete(tmp_path) -> None:
    key = generate_vault_key()
    vault = EncryptedFileProfileVault(key, tmp_path / "vault")
    profile_id = vault.create(_profile())
    vault.delete(profile_id)
    assert vault.get(profile_id) is None
    assert vault.exists(profile_id) is False


def test_plaintext_sensitive_values_absent_from_persisted_file(tmp_path) -> None:
    key = generate_vault_key()
    vault = EncryptedFileProfileVault(key, tmp_path / "vault")
    profile_id = vault.create(_profile())
    persisted = (tmp_path / "vault" / f"{profile_id}.enc").read_bytes()
    text = persisted.decode("latin-1", errors="ignore")
    for marker in (SYNTHETIC_LICENCE, SYNTHETIC_VIN, SYNTHETIC_POSTAL, "Test Applicant", "1990-01-01"):
        assert marker not in text
    assert text != ""  # file exists with content


def test_encrypted_requires_key(tmp_path) -> None:
    with pytest.raises(ValueError):
        EncryptedFileProfileVault(b"", tmp_path / "vault")


def test_key_from_secret_is_stable() -> None:
    key = vault_key_from_secret("some-env-secret")
    assert key == vault_key_from_secret("some-env-secret")
    assert len(key) == 44  # 32 bytes -> base64


def test_vault_uses_temporary_directory(tmp_path) -> None:
    key = generate_vault_key()
    vault = EncryptedFileProfileVault(key, tmp_path / "vault")
    vault.create(_profile())
    files = list((tmp_path / "vault").glob("*.enc"))
    assert len(files) == 1
