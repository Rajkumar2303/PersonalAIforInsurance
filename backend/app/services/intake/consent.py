"""Consent service (Issue #5).

Records and queries structured consent receipts. Receipts hold PATHS and
metadata only - never field values. A route decision is recorded once per
session+route (no repeated consent asks during the same valid decision
context).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Optional

from ...models.intake.consent import ConsentReceipt, ConsentScope, RouteDisclosureConsent
from ...models.intake.session import IntakeSession

logger = logging.getLogger(__name__)


class ConsentService:
    """Ephemeral, deterministic consent receipt store (Issue #5)."""

    def __init__(self) -> None:
        self._receipts: dict[str, ConsentReceipt] = {}  # consent_id -> receipt
        self._by_session: dict[str, list[str]] = {}  # session_id -> [consent_id]

    # --- recording ---------------------------------------------------

    def record(
        self,
        session: IntakeSession,
        scope: ConsentScope,
        *,
        granted: bool,
        route_registry_id: Optional[str] = None,
        paths: Optional[list[str]] = None,
        subject_reference: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> ConsentReceipt:
        now = dt.datetime.now(dt.timezone.utc)
        receipt = ConsentReceipt(
            consent_id=uuid.uuid4().hex,
            session_id=session.session_id,
            profile_id=session.profile_id,
            scope=scope,
            route_registry_id=route_registry_id,
            canonical_field_paths=list(dict.fromkeys(paths or [])),  # paths, not values
            granted=granted,
            timestamp=now,
            subject_reference=subject_reference,
            purpose=purpose,
        )
        self._receipts[receipt.consent_id] = receipt
        self._by_session.setdefault(session.session_id, []).append(receipt.consent_id)
        logger.info(
            "consent recorded",
            extra={
                "workflow": "intake_consent",
                "workflow_stage": "record",
                "status": "ok",
                "consent_scope": receipt.scope.value,
                "granted": receipt.granted,
            },
        )
        return receipt

    # --- queries -----------------------------------------------------

    def get(self, consent_id: str) -> Optional[ConsentReceipt]:
        return self._receipts.get(consent_id)

    def for_session(self, session_id: str) -> list[ConsentReceipt]:
        return [self._receipts[cid] for cid in self._by_session.get(session_id, [])]

    def has_active(
        self,
        session_id: str,
        scope: ConsentScope,
        *,
        route_registry_id: Optional[str] = None,
        subject_reference: Optional[str] = None,
    ) -> bool:
        """True when a granted, non-revoked receipt exists for the scope."""
        for receipt in self.for_session(session_id):
            if (
                receipt.scope is scope
                and receipt.granted
                and receipt.revoked_at is None
                and (route_registry_id is None or receipt.route_registry_id == route_registry_id)
                and (subject_reference is None or receipt.subject_reference == subject_reference)
            ):
                return True
        return False

    def route_decision_exists(self, session_id: str, route_registry_id: str) -> bool:
        """A route-disclosure decision (grant or deny) already exists."""
        return any(
            r.scope is ConsentScope.ROUTE_DISCLOSURE
            and r.route_registry_id == route_registry_id
            and r.revoked_at is None
            for r in self.for_session(session_id)
        )

    def route_consent(self, session_id: str, route_registry_id: str) -> Optional[RouteDisclosureConsent]:
        """Return the active route-disclosure consent for a route, if any."""
        for receipt in self.for_session(session_id):
            if (
                receipt.scope is ConsentScope.ROUTE_DISCLOSURE
                and receipt.route_registry_id == route_registry_id
                and receipt.revoked_at is None
            ):
                return RouteDisclosureConsent(
                    consent_id=receipt.consent_id,
                    route_registry_id=receipt.route_registry_id,
                    registry_name=receipt.purpose,
                    canonical_field_paths=receipt.canonical_field_paths,
                    granted=receipt.granted,
                    timestamp=receipt.timestamp,
                    revoked_at=receipt.revoked_at,
                )
        return None

    # --- revocation --------------------------------------------------

    def revoke(self, consent_id: str) -> Optional[ConsentReceipt]:
        receipt = self._receipts.get(consent_id)
        if receipt is None or receipt.revoked_at is not None:
            return receipt
        receipt = receipt.model_copy(update={"revoked_at": dt.datetime.now(dt.timezone.utc)})
        self._receipts[consent_id] = receipt
        return receipt

    def delete_session(self, session_id: str) -> None:
        for consent_id in self._by_session.pop(session_id, []):
            self._receipts.pop(consent_id, None)
