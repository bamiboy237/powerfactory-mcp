from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from powerfactory_agent.domain.gateway import AttributeKind, AttributeSelector
from powerfactory_agent.domain.models import VersionedName
from powerfactory_agent.domain.reconciliation import (
    ManualDisposition,
    ObservationOutcome,
    ObservationSource,
    ReconciliationClassification,
    ReconciliationObservation,
    ReconciliationRecord,
    WriteAheadIntent,
)
from powerfactory_agent.domain.values import (
    ConfigurationKey,
    ContentDigest,
    LiveStateFingerprint,
    ProductIdentity,
    Quantity,
    WorkspaceRevision,
    WorkflowVersion,
)


INTENT_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
WORKFLOW_ID = "33333333-3333-4333-8333-333333333333"
LEASE_ID = "44444444-4444-4444-8444-444444444444"
WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
PRODUCT_ID = "66666666-6666-4666-8666-666666666666"
LOCATOR_ID = "77777777-7777-4777-8777-777777777777"
OWNER_ID = "88888888-8888-4888-8888-888888888888"
CORRELATION_ID = "99999999-9999-4999-8999-999999999999"
OBSERVATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ATTEMPT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RECONCILIATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def digest(value: str) -> ContentDigest:
    return ContentDigest(f"content:v1:sha256:{value * 64}")


class ReconciliationContractTests(unittest.TestCase):
    def intent(self, **overrides: object) -> WriteAheadIntent:
        values: dict[str, object] = {
            "intent_id": INTENT_ID,
            "operation_id": OPERATION_ID,
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WorkflowVersion(WORKFLOW_ID, 8),
            "idempotency_key": "apply-change-1",
            "execution_id": "10101010-1010-4010-8010-101010101010",
            "lease_id": LEASE_ID,
            "fencing_token": 11,
            "workspace_id": WORKSPACE_ID,
            "workspace_revision": WorkspaceRevision(WORKSPACE_ID, 4),
            "product_identity": ProductIdentity(PRODUCT_ID),
            "locator_version_id": LOCATOR_ID,
            "attribute": AttributeSelector(AttributeKind.ACTIVE_POWER, VersionedName("attribute-selector", "v1")),
            "expected_before": Quantity(Decimal("12"), "MW"),
            "proposed_after": Quantity(Decimal("14"), "MW"),
            "configuration_key": ConfigurationKey(f"configuration-key:v1:sha256:{'a' * 64}"),
            "live_state_fingerprint": LiveStateFingerprint(f"live-state-fingerprint:v1:sha256:{'b' * 64}"),
            "request_digest": digest("c"),
            "policy_versions": (VersionedName("area-load-scaling", "v1"),),
            "owner_instance_id": OWNER_ID,
            "session_id": "session-v1:single-owner",
            "correlation_id": CORRELATION_ID,
            "attempt_number": 1,
            "created_at": NOW,
            "intent_digest": digest("d"),
        }
        values.update(overrides)
        return WriteAheadIntent(**values)  # type: ignore[arg-type]

    def observation(self, **overrides: object) -> ReconciliationObservation:
        values: dict[str, object] = {
            "observation_id": OBSERVATION_ID,
            "intent_id": INTENT_ID,
            "operation_id": OPERATION_ID,
            "attempt_id": ATTEMPT_ID,
            "source": ObservationSource.RECOVERY,
            "outcome": ObservationOutcome.VALUE_OBSERVED,
            "observed_at": NOW + timedelta(seconds=1),
            "diagnostic_reference": "evidence:v1:fresh-live-read",
            "observed_value": Quantity(Decimal("14"), "MW"),
            "configuration_key": ConfigurationKey(f"configuration-key:v1:sha256:{'a' * 64}"),
            "live_state_fingerprint": LiveStateFingerprint(f"live-state-fingerprint:v1:sha256:{'b' * 64}"),
            "workspace_revision": WorkspaceRevision(WORKSPACE_ID, 4),
        }
        values.update(overrides)
        return ReconciliationObservation(**values)  # type: ignore[arg-type]

    def record(self, **overrides: object) -> ReconciliationRecord:
        values: dict[str, object] = {
            "reconciliation_id": RECONCILIATION_ID,
            "intent_id": INTENT_ID,
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WorkflowVersion(WORKFLOW_ID, 9),
            "classification": ReconciliationClassification.AFTER_OBSERVED,
            "observation_ids": (OBSERVATION_ID,),
            "classified_at": NOW + timedelta(seconds=2),
            "evidence_reference": "evidence:v1:after-observed",
        }
        values.update(overrides)
        return ReconciliationRecord(**values)  # type: ignore[arg-type]

    def test_records_are_immutable(self) -> None:
        for record in (self.intent(), self.observation(), self.record()):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, "intent_id", OPERATION_ID)

    def test_intent_binds_workflow_workspace_and_exact_quantities(self) -> None:
        self.assertEqual("MW", self.intent().proposed_after.unit)
        with self.assertRaises(ValueError):
            self.intent(workflow_version=WorkflowVersion(PRODUCT_ID, 8))
        with self.assertRaises(ValueError):
            self.intent(workspace_revision=WorkspaceRevision(PRODUCT_ID, 4))
        with self.assertRaises(ValueError):
            self.intent(proposed_after=Quantity(Decimal("14"), "Mvar"))
        with self.assertRaises(ValueError):
            self.intent(fencing_token=0)
        with self.assertRaises(ValueError):
            self.intent(policy_versions=(VersionedName("area-load-scaling", "v1"), VersionedName("area-load-scaling", "v1")))
        with self.assertRaises(ValueError):
            self.intent(policy_versions=(VersionedName("area-load-scaling", "v1"), VersionedName("area-load-scaling", "v2")))

    def test_observation_requires_complete_fresh_evidence_for_a_value(self) -> None:
        self.assertEqual(ObservationSource.RECOVERY, self.observation().source)
        with self.assertRaises(ValueError):
            self.observation(observed_value=None)
        with self.assertRaises(ValueError):
            self.observation(live_state_fingerprint=None)
        with self.assertRaises(ValueError):
            self.observation(
                outcome=ObservationOutcome.ENGINE_UNAVAILABLE,
                observed_value=Quantity(Decimal("14"), "MW"),
            )
        with self.assertRaises(ValueError):
            self.observation(
                outcome=ObservationOutcome.ENGINE_UNAVAILABLE,
                observed_value=None,
            )

    def test_reconciliation_quarantines_divergence_and_unavailability(self) -> None:
        with self.assertRaises(ValueError):
            self.record(classification=ReconciliationClassification.DIVERGED)
        self.assertEqual(
            ReconciliationClassification.UNAVAILABLE,
            self.record(
                classification=ReconciliationClassification.UNAVAILABLE,
                quarantine_reference="quarantine:v1:engine-unavailable",
            ).classification,
        )
        with self.assertRaises(ValueError):
            self.record(manual_disposition=ManualDisposition.CONFIRM_AFTER_OBSERVED)
        with self.assertRaises(ValueError):
            self.record(operator_principal_reference="local-operator")
        self.assertEqual(
            ManualDisposition.AUTHORIZE_COMPENSATION,
            self.record(
                manual_disposition=ManualDisposition.AUTHORIZE_COMPENSATION,
                operator_principal_reference="local-operator",
                completed_at=NOW + timedelta(seconds=3),
            ).manual_disposition,
        )
        with self.assertRaises(ValueError):
            self.record(completed_at=NOW)


if __name__ == "__main__":
    unittest.main()
