"""Tests for human checkpoint controls (Issue #5, section 33).

Checkpoint definitions are structured, safe instructions for future browser/
voice agents. Signature/payment/purchase/binding/renewal/cancellation must be
marked ``must_not_automate``. No browser operation occurs.
"""

from __future__ import annotations

from app.models.intake.checkpoints import HumanCheckpointKind
from app.services.intake.checkpoints import CheckpointService


def test_identity_lookup_requires_human() -> None:
    requirement = CheckpointService().evaluate(HumanCheckpointKind.IDENTITY_LOOKUP)
    assert requirement is not None
    assert requirement.requires_explicit_human_checkpoint is True
    assert requirement.must_not_automate is False


def test_consent_attestation_requires_human() -> None:
    requirement = CheckpointService().evaluate(HumanCheckpointKind.CONSENT_ATTESTATION)
    assert requirement.requires_explicit_human_checkpoint is True


def test_application_declaration_requires_human() -> None:
    requirement = CheckpointService().evaluate(HumanCheckpointKind.APPLICATION_DECLARATION)
    assert requirement.requires_explicit_human_checkpoint is True


def test_signature_must_not_be_automated() -> None:
    requirement = CheckpointService().evaluate(HumanCheckpointKind.SIGNATURE)
    assert requirement.must_not_automate is True


def test_payment_must_not_be_automated() -> None:
    requirement = CheckpointService().evaluate(HumanCheckpointKind.PAYMENT)
    assert requirement.must_not_automate is True


def test_purchase_must_not_be_automated() -> None:
    requirement = CheckpointService().evaluate(HumanCheckpointKind.PURCHASE)
    assert requirement.must_not_automate is True


def test_binding_renewal_cancellation_not_automated() -> None:
    for kind in (
        HumanCheckpointKind.POLICY_BINDING,
        HumanCheckpointKind.RENEWAL,
        HumanCheckpointKind.CANCELLATION,
    ):
        requirement = CheckpointService().evaluate(kind)
        assert requirement.must_not_automate is True


def test_all_kinds_have_definitions() -> None:
    service = CheckpointService()
    for kind in service.kinds():
        assert service.evaluate(kind) is not None


def test_engine_evaluates_checkpoint(tmp_path) -> None:
    from app.models.insurance.enums import InsuranceType

    from intake_helpers import make_engine

    engine = make_engine(tmp_path)
    engine.create_session(InsuranceType.AUTO)
    requirement = engine.evaluate_checkpoint(HumanCheckpointKind.PAYMENT)
    assert requirement.must_not_automate is True
