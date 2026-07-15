from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from powerfactory_agent.domain.gateway import AttributeKind, AttributeSelector
from powerfactory_agent.domain.models import VersionedName
from powerfactory_agent.domain.reconciliation import (
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
from powerfactory_agent.domain.workflow import WorkflowRecord, WorkflowState
from powerfactory_agent.persistence.database import SQLiteDatabase
from powerfactory_agent.persistence.reconciliation_store import (
    ReconciliationClassificationError,
    ReconciliationIntentConflictError,
    ReconciliationObservationConflictError,
    ReconciliationStore,
)
from powerfactory_agent.persistence.workflow_store import WorkflowStore


WORKFLOW_ID = "33333333-3333-4333-8333-333333333333"
INTENT_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
LEASE_ID = "44444444-4444-4444-8444-444444444444"
WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
PRODUCT_ID = "66666666-6666-4666-8666-666666666666"
LOCATOR_ID = "77777777-7777-4777-8777-777777777777"
OWNER_ID = "88888888-8888-4888-8888-888888888888"
CORRELATION_ID = "99999999-9999-4999-8999-999999999999"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def digest(value: str) -> ContentDigest:
    return ContentDigest(f"content:v1:sha256:{value * 64}")


class ReconciliationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.temporary_directory.name) / "reconciliation.sqlite3")
        self.workflow_store = WorkflowStore(self.database)
        self.store = ReconciliationStore(self.database)
        self.workflow_store.record(
            WorkflowRecord(
                workflow_id=WORKFLOW_ID,
                state=WorkflowState.EXECUTING,
                workflow_version=WorkflowVersion(WORKFLOW_ID, 8),
                operation_specification=VersionedName("area-load-scaling", "v1"),
                configuration_key=self.configuration_key,
                proposal_digest=digest("e"),
                created_at=NOW,
                updated_at=NOW,
                latest_operation_id=OPERATION_ID,
            )
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @property
    def configuration_key(self) -> ConfigurationKey:
        return ConfigurationKey(f"configuration-key:v1:sha256:{'a' * 64}")

    @property
    def fingerprint(self) -> LiveStateFingerprint:
        return LiveStateFingerprint(f"live-state-fingerprint:v1:sha256:{'b' * 64}")

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
            "configuration_key": self.configuration_key,
            "live_state_fingerprint": self.fingerprint,
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

    def observation(
        self,
        observation_id: str,
        *,
        value: Quantity | None = Quantity(Decimal("12"), "MW"),
        outcome: ObservationOutcome = ObservationOutcome.VALUE_OBSERVED,
        **overrides: object,
    ) -> ReconciliationObservation:
        values: dict[str, object] = {
            "observation_id": observation_id,
            "intent_id": INTENT_ID,
            "operation_id": OPERATION_ID,
            "attempt_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "source": ObservationSource.RECOVERY,
            "outcome": outcome,
            "observed_at": NOW + timedelta(seconds=1),
            "diagnostic_reference": "evidence:v1:fresh-live-read",
            "observed_value": value,
            "configuration_key": self.configuration_key if value is not None else None,
            "live_state_fingerprint": self.fingerprint if value is not None else None,
            "workspace_revision": WorkspaceRevision(WORKSPACE_ID, 4) if value is not None else None,
        }
        values.update(overrides)
        return ReconciliationObservation(**values)  # type: ignore[arg-type]

    def record(
        self,
        reconciliation_id: str,
        observation_ids: tuple[str, ...],
        classification: ReconciliationClassification,
    ) -> ReconciliationRecord:
        values: dict[str, object] = {
            "reconciliation_id": reconciliation_id,
            "intent_id": INTENT_ID,
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WorkflowVersion(WORKFLOW_ID, 8),
            "classification": classification,
            "observation_ids": observation_ids,
            "classified_at": NOW + timedelta(seconds=2),
            "evidence_reference": "evidence:v1:classification",
        }
        if classification in (ReconciliationClassification.DIVERGED, ReconciliationClassification.UNAVAILABLE):
            values["quarantine_reference"] = "quarantine:v1:required"
        return ReconciliationRecord(**values)  # type: ignore[arg-type]

    def test_intent_is_idempotent_and_survives_restart(self) -> None:
        intent = self.intent()
        self.assertEqual(intent, self.store.commit_intent(intent))
        self.assertEqual(intent, self.store.commit_intent(intent))
        restarted = ReconciliationStore(SQLiteDatabase(self.database.path))
        self.assertEqual((intent,), restarted.intents_for_workflow(WORKFLOW_ID))
        with self.assertRaises(ReconciliationIntentConflictError):
            self.store.commit_intent(self.intent(request_digest=digest("f")))

    def test_intent_requires_current_executing_workflow(self) -> None:
        with self.assertRaises(ReconciliationIntentConflictError):
            self.store.commit_intent(self.intent(workflow_version=WorkflowVersion(WORKFLOW_ID, 7)))

    def test_observations_are_append_only_and_bound_to_the_intent(self) -> None:
        self.store.commit_intent(self.intent())
        observation = self.observation("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.assertEqual(observation, self.store.append_observation(observation))
        self.assertEqual(observation, self.store.append_observation(observation))
        with self.assertRaises(ReconciliationObservationConflictError):
            self.store.append_observation(
                self.observation("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", value=Quantity(Decimal("14"), "MW"))
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute("UPDATE reconciliation_observations SET source = 'live_read'")

    def test_classification_requires_fresh_exact_or_fail_closed_evidence(self) -> None:
        self.store.commit_intent(self.intent())
        before_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        after_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        third_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        unavailable_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        self.store.append_observation(self.observation(before_id))
        self.store.append_observation(self.observation(after_id, value=Quantity(Decimal("14"), "MW")))
        self.store.append_observation(self.observation(third_id, value=Quantity(Decimal("13"), "MW")))
        self.store.append_observation(
            self.observation(
                unavailable_id,
                value=None,
                outcome=ObservationOutcome.ENGINE_UNAVAILABLE,
            )
        )
        before = self.record(
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            (before_id,),
            ReconciliationClassification.BEFORE,
        )
        self.assertEqual(before, self.store.record_classification(before))
        after = self.record(
            "12121212-1212-4212-8212-121212121212",
            (after_id,),
            ReconciliationClassification.AFTER_OBSERVED,
        )
        self.assertEqual(after, self.store.record_classification(after))
        diverged = self.record(
            "13131313-1313-4313-8313-131313131313",
            (third_id,),
            ReconciliationClassification.DIVERGED,
        )
        self.assertEqual(diverged, self.store.record_classification(diverged))
        unavailable = self.record(
            "14141414-1414-4414-8414-141414141414",
            (unavailable_id,),
            ReconciliationClassification.UNAVAILABLE,
        )
        self.assertEqual(unavailable, self.store.record_classification(unavailable))
        with self.assertRaises(ReconciliationClassificationError):
            self.store.record_classification(
                self.record(
                    "15151515-1515-4515-8515-151515151515",
                    (before_id,),
                    ReconciliationClassification.AFTER_OBSERVED,
                )
            )


if __name__ == "__main__":
    unittest.main()
