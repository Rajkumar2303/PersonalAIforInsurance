"""Checkpoint service (Issue #5).

Returns structured, deterministic control signals for future browser/voice
agents. Signature/declaration/payment/purchase/binding/renewal/cancellation are
marked ``must_not_automate`` - the automation must never perform those actions.
The applicant must personally accept an application declaration; an identity /
database lookup is resumable but only after explicit participant approval. This
module performs NO browser action and defines NO quote terminal statuses.
"""

from __future__ import annotations

from typing import Optional

from ...models.intake.checkpoints import CheckpointRequirement, HumanCheckpointKind

# Deterministic static safety definitions (control metadata, not questions).
_CHECKPOINT_DEFINITIONS: dict[HumanCheckpointKind, tuple[str, str, bool]] = {
    # kind: (label, reason, must_not_automate)
    HumanCheckpointKind.IDENTITY_LOOKUP: (
        "Identity verification",
        "Verify the applicant's identity before continuing.",
        False,
    ),
    HumanCheckpointKind.CONSENT_ATTESTATION: (
        "Consent attestation",
        "Obtain explicit consent/attestation before collecting or sharing data.",
        False,
    ),
    HumanCheckpointKind.APPLICATION_DECLARATION: (
        "Application declaration",
        "The applicant must review and attest to the application declarations - "
        "automation must not accept them on the applicant's behalf.",
        True,
    ),
    HumanCheckpointKind.SIGNATURE: (
        "Signature",
        "A signature is required - automation must not sign on the applicant's behalf.",
        True,
    ),
    HumanCheckpointKind.PAYMENT: (
        "Payment",
        "Payment must be completed by the applicant - automation must not pay.",
        True,
    ),
    HumanCheckpointKind.PURCHASE: (
        "Purchase / binding",
        "Binding the policy is a purchase transition - automation must not proceed.",
        True,
    ),
    HumanCheckpointKind.POLICY_BINDING: (
        "Policy binding",
        "Binding a policy must be an explicit human action.",
        True,
    ),
    HumanCheckpointKind.RENEWAL: (
        "Renewal",
        "Renewal changes the policy - automation must not proceed.",
        True,
    ),
    HumanCheckpointKind.CANCELLATION: (
        "Cancellation",
        "Cancellation modifies the policy - automation must not proceed.",
        True,
    ),
}


class CheckpointService:
    """Deterministic provider of human-checkpoint control signals."""

    def evaluate(self, kind: HumanCheckpointKind) -> Optional[CheckpointRequirement]:
        definition = _CHECKPOINT_DEFINITIONS.get(kind)
        if definition is None:
            return None
        label, reason, must_not_automate = definition
        return CheckpointRequirement(
            kind=kind,
            label=label,
            reason=reason,
            requires_explicit_human_checkpoint=True,
            must_not_automate=must_not_automate,
        )

    def kinds(self) -> list[HumanCheckpointKind]:
        return list(HumanCheckpointKind)
