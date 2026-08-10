"""Issue #8 - data-driven recovery policy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.recovery import RecoveryPolicy
from app.services.recovery.policy import RecoveryPolicyLoader, RecoveryPolicyLoadError


def test_loader_defaults_when_file_missing(tmp_path: Path):
    loader = RecoveryPolicyLoader(policy_dir=tmp_path / "recovery")
    policy = loader.load()
    assert policy.max_attempts_per_route == 2
    assert policy.max_transient_retries == 1
    assert policy.max_attempts_per_rate_source == 3


def test_loader_reads_data_overrides(tmp_path: Path):
    directory = tmp_path / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_policy.json").write_text(
        json.dumps({"default": {"max_attempts_per_route": 3, "max_attempts_per_rate_source": 5}}),
        encoding="utf-8",
    )
    policy = RecoveryPolicyLoader(policy_dir=directory).load()
    assert policy.max_attempts_per_route == 3
    assert policy.max_attempts_per_rate_source == 5
    assert policy.max_transient_retries == 1  # untouched default preserved


def test_loader_rejects_invalid_json(tmp_path: Path):
    directory = tmp_path / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_policy.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RecoveryPolicyLoadError):
        RecoveryPolicyLoader(policy_dir=directory)


def test_policy_data_change_needs_no_code(tmp_path: Path):
    """Changing policy data changes behavior - proven in test_recovery_dynamic;
    here we just confirm a data override produces a different policy object."""
    directory = tmp_path / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto_policy.json").write_text(
        json.dumps({"default": {"max_attempts_per_route": 4}}), encoding="utf-8"
    )
    assert RecoveryPolicyLoader(policy_dir=directory).load().max_attempts_per_route == 4
