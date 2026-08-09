"""Profile vault abstraction (Issue #5).

Raw applicant data lives ONLY behind a vault. Workflow components carry an
opaque ``profile_id`` and safe metadata - never the raw ``InsuranceProfile``.

``ProfileVault`` is a Protocol so PostgreSQL or another backend can replace the
implementations without changing ``IntakeEngine``/``IntakeService``.

Two implementations:

- ``InMemoryProfileVault``: ephemeral dict (default; also used by tests).
- ``EncryptedFileProfileVault``: Fernet-encrypted-at-rest files. The key comes
  ONLY from environment/config (``INTAKE_VAULT_KEY``) - never committed. The
  data directory is gitignored and plaintext PII is never written to disk.

Issue #5 does NOT build a database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken

from ...core.config import BACKEND_ROOT, get_settings
from ...models.insurance.profile import InsuranceProfile

logger = logging.getLogger(__name__)

_VAULT_SUFFIX = ".enc"


@runtime_checkable
class ProfileVault(Protocol):
    """Abstract storage interface for canonical insurance profiles."""

    def create(self, profile: InsuranceProfile) -> str:
        """Persist a new profile and return its opaque profile_id."""
        ...

    def get(self, profile_id: str) -> Optional[InsuranceProfile]:
        """Return the profile, or None when unknown/deleted."""
        ...

    def update(self, profile_id: str, profile: InsuranceProfile) -> InsuranceProfile:
        """Overwrite the profile at ``profile_id`` (must exist)."""
        ...

    def delete(self, profile_id: str) -> None:
        """Remove the profile. Idempotent."""
        ...

    def exists(self, profile_id: str) -> bool:
        """True when a profile with this id exists."""
        ...


class ProfileNotFoundError(KeyError):
    """Raised when updating a profile that does not exist."""


class InMemoryProfileVault:
    """Ephemeral, process-lifetime profile store (dev/tests default)."""

    def __init__(self) -> None:
        self._profiles: dict[str, InsuranceProfile] = {}

    def create(self, profile: InsuranceProfile) -> str:
        profile_id = uuid.uuid4().hex
        self._profiles[profile_id] = profile
        return profile_id

    def get(self, profile_id: str) -> Optional[InsuranceProfile]:
        return self._profiles.get(profile_id)

    def update(self, profile_id: str, profile: InsuranceProfile) -> InsuranceProfile:
        if profile_id not in self._profiles:
            raise ProfileNotFoundError(profile_id)
        self._profiles[profile_id] = profile
        return profile

    def delete(self, profile_id: str) -> None:
        self._profiles.pop(profile_id, None)

    def exists(self, profile_id: str) -> bool:
        return profile_id in self._profiles


def vault_key_from_secret(secret: str) -> bytes:
    """Derive a Fernet key from an arbitrary environment secret.

    Uses SHA-256 to map the secret to exactly 32 URL-safe base64 bytes - the
    Fernet key format. No custom encryption is implemented.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def generate_vault_key() -> bytes:
    """Generate a fresh Fernet key (used by tests; never committed)."""
    return Fernet.generate_key()


class EncryptedFileProfileVault:
    """Fernet-encrypted-at-rest profile store.

    Each profile is one ``<profile_id>.enc`` file containing the JSON-serialized
    profile, encrypted. Plaintext PII is never written to disk.
    """

    def __init__(self, key: bytes, directory: Path) -> None:
        if not key:
            raise ValueError("EncryptedFileProfileVault requires a Fernet key (from env, never hardcoded)")
        self._fernet = Fernet(key)
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_secret(cls, secret: str, directory: Path) -> "EncryptedFileProfileVault":
        return cls(vault_key_from_secret(secret), directory)

    def _path(self, profile_id: str) -> Path:
        return self._directory / f"{profile_id}{_VAULT_SUFFIX}"

    def create(self, profile: InsuranceProfile) -> str:
        profile_id = uuid.uuid4().hex
        self._write(profile_id, profile)
        return profile_id

    def _write(self, profile_id: str, profile: InsuranceProfile) -> None:
        payload = profile.model_dump_json().encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        self._path(profile_id).write_bytes(encrypted)

    def get(self, profile_id: str) -> Optional[InsuranceProfile]:
        path = self._path(profile_id)
        if not path.exists():
            return None
        try:
            decrypted = self._fernet.decrypt(path.read_bytes())
            return InsuranceProfile.model_validate_json(decrypted)
        except InvalidToken as exc:
            logger.error(
                "failed to decrypt profile (invalid token)",
                extra={"workflow": "intake_vault", "workflow_stage": "get", "status": "error"},
            )
            raise ValueError("failed to decrypt profile") from exc

    def update(self, profile_id: str, profile: InsuranceProfile) -> InsuranceProfile:
        if not self.exists(profile_id):
            raise ProfileNotFoundError(profile_id)
        self._write(profile_id, profile)
        return profile

    def delete(self, profile_id: str) -> None:
        path = self._path(profile_id)
        if path.exists():
            path.unlink()

    def exists(self, profile_id: str) -> bool:
        return self._path(profile_id).exists()


def default_vault_dir() -> Path:
    settings = get_settings()
    if settings.intake_vault_dir:
        return Path(settings.intake_vault_dir)
    return BACKEND_ROOT / "data" / "vault"


def build_profile_vault() -> ProfileVault:
    """Build the default vault from settings.

    - When ``INTAKE_VAULT_KEY`` is set: encrypted-at-rest file vault.
    - Otherwise: in-memory vault (ephemeral, no persistence).
    """
    settings = get_settings()
    if settings.intake_vault_key:
        return EncryptedFileProfileVault.from_secret(settings.intake_vault_key, default_vault_dir())
    return InMemoryProfileVault()
