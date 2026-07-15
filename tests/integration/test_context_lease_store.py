from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    AuditActorClass,
    AuditEventType,
    ConfigurationKey,
    ContentDigest,
    LeaseEventType,
    LeaseMode,
    LeaseState,
    VersionedName,
    WorkflowRecord,
    WorkflowState,
    WorkflowVersion,
)
from powerfactory_agent.persistence import (
    ContextLeaseStore,
    LeaseBusyError,
    LeaseFenceRejectedError,
    SQLiteDatabase,
    WorkflowStore,
)


WORKFLOW_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKFLOW_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OWNER_A = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OWNER_B = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OPERATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def digest(character: str) -> ContentDigest:
    return ContentDigest(f"content:v1:sha256:{character * 64}")


class ContextLeaseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.directory.name) / "lease.sqlite")
        self.workflows = WorkflowStore(self.database)
        self.scope = digest("a")
        self.configuration = ConfigurationKey("configuration-key:v1:sha256:" + "b" * 64)
        for workflow_id, proposal_character in ((WORKFLOW_A, "c"), (WORKFLOW_B, "d")):
            self.workflows.record(
                WorkflowRecord(
                    workflow_id=workflow_id,
                    state=WorkflowState.NEW,
                    workflow_version=WorkflowVersion(workflow_id, 0),
                    operation_specification=VersionedName("area-load-scaling", "v1"),
                    configuration_key=self.configuration,
                    proposal_digest=digest(proposal_character),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        self.store = ContextLeaseStore(self.database)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def acquire(self, *, workflow_id: str = WORKFLOW_A, mode: LeaseMode = LeaseMode.PREVIEW,
                owner: str = OWNER_A, issued_at: datetime = NOW):
        return self.store.acquire(
            mode=mode,
            service_scope_digest=self.scope,
            configuration_key=self.configuration,
            workflow_id=workflow_id,
            expected_workflow_version=WorkflowVersion(workflow_id, 0),
            owner_instance_id=owner,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=1),
            reason="workflow context admission",
            evidence_reference="evidence:v1:lease-admission",
        )

    def test_exclusive_preview_release_and_reacquire_never_reuses_a_fence(self) -> None:
        preview = self.acquire()
        with self.assertRaises(LeaseBusyError):
            self.acquire(workflow_id=WORKFLOW_B, owner=OWNER_B)

        self.store.release_for_authorization(
            lease_id=preview.lease_id,
            fencing_token=preview.fencing_token,
            owner_instance_id=OWNER_A,
            workflow_version=preview.workflow_version,
            occurred_at=NOW + timedelta(seconds=10),
            reason="preview persisted before authorization wait",
            evidence_reference="evidence:v1:preview-persisted",
        )
        self.assertIsNone(
            self.store.lease(
                service_scope_digest=self.scope, configuration_key=self.configuration
            )
        )

        execution = self.acquire(
            workflow_id=WORKFLOW_B,
            mode=LeaseMode.EXECUTION,
            owner=OWNER_B,
            issued_at=NOW + timedelta(seconds=11),
        )
        self.assertEqual(preview.fencing_token + 1, execution.fencing_token)
        restarted = ContextLeaseStore(SQLiteDatabase(self.database.path))
        self.assertEqual(
            execution,
            restarted.lease(service_scope_digest=self.scope, configuration_key=self.configuration),
        )
        self.assertEqual(
            (
                "acquire_attempted",
                "acquired",
                "released_for_authorization",
                "acquire_attempted",
                "acquired",
            ),
            tuple(
                event.event_type.value
                for event in restarted.lease_events(
                    service_scope_digest=self.scope, configuration_key=self.configuration
                )
            ),
        )

    def test_stale_owner_or_token_is_rejected_without_starting_an_atomic_call(self) -> None:
        lease = self.acquire(mode=LeaseMode.EXECUTION)
        with self.assertRaises(LeaseFenceRejectedError):
            self.store.start_atomic_call(
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token + 1,
                owner_instance_id=OWNER_B,
                workflow_version=lease.workflow_version,
                operation_id=OPERATION_ID,
                occurred_at=NOW + timedelta(seconds=1),
                reason="stale dispatch rejected",
                evidence_reference="evidence:v1:stale-fence",
            )
        current = self.store.lease(
            service_scope_digest=self.scope, configuration_key=self.configuration
        )
        self.assertEqual(lease, current)
        self.assertIsNone(current.operation_id if current is not None else None)
        self.assertEqual(
            LeaseEventType.STALE_FENCE_REJECTED,
            self.store.lease_events(
                service_scope_digest=self.scope, configuration_key=self.configuration
            )[-1].event_type,
        )

    def test_atomic_call_rejects_a_workflow_version_changed_after_admission(self) -> None:
        lease = self.acquire(mode=LeaseMode.EXECUTION)
        self.workflows.transition(
            workflow_id=WORKFLOW_A,
            command_name="start_preview",
            idempotency_key="advance-workflow-version",
            request_digest=digest("e"),
            expected_workflow_version=lease.workflow_version,
            target_state=WorkflowState.PREVIEWING,
            transition_event_type=AuditEventType.PREVIEW_STARTED,
            actor_class=AuditActorClass.WORKFLOW_SERVICE,
            evidence_reference="evidence:v1:workflow-cas",
            occurred_at=NOW + timedelta(seconds=1),
        )
        with self.assertRaises(LeaseFenceRejectedError):
            self.store.start_atomic_call(
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token,
                owner_instance_id=OWNER_A,
                workflow_version=lease.workflow_version,
                operation_id=OPERATION_ID,
                occurred_at=NOW + timedelta(seconds=2),
                reason="workflow version changed",
                evidence_reference="evidence:v1:workflow-cas",
            )
        self.assertIsNone(
            self.store.lease(
                service_scope_digest=self.scope, configuration_key=self.configuration
            ).operation_id
        )

    def test_expiry_distinguishes_no_effect_from_in_flight_and_is_restart_safe(self) -> None:
        preview = self.acquire()
        expired = self.store.expire(
            service_scope_digest=self.scope,
            configuration_key=self.configuration,
            occurred_at=NOW + timedelta(minutes=1),
            reason="holder deadline elapsed",
            evidence_reference="evidence:v1:clock",
        )
        self.assertEqual(LeaseState.EXPIRED, expired.state)
        self.assertIsNone(
            self.store.lease(
                service_scope_digest=self.scope, configuration_key=self.configuration
            )
        )
        later = self.acquire(
            workflow_id=WORKFLOW_B,
            mode=LeaseMode.ROLLBACK,
            owner=OWNER_B,
            issued_at=NOW + timedelta(minutes=1, seconds=1),
        )
        self.assertGreater(later.fencing_token, preview.fencing_token)
        started = self.store.start_atomic_call(
            lease_id=later.lease_id,
            fencing_token=later.fencing_token,
            owner_instance_id=OWNER_B,
            workflow_version=later.workflow_version,
            operation_id=OPERATION_ID,
            occurred_at=NOW + timedelta(minutes=1, seconds=2),
            reason="durable mutation intent",
            evidence_reference="evidence:v1:atomic-intent",
        )
        self.assertEqual(OPERATION_ID, started.operation_id)
        in_flight = self.store.expire(
            service_scope_digest=self.scope,
            configuration_key=self.configuration,
            occurred_at=NOW + timedelta(minutes=2, seconds=1),
            reason="atomic call exceeded holder deadline",
            evidence_reference="evidence:v1:clock",
        )
        self.assertEqual(LeaseState.IN_FLIGHT_EXPIRED, in_flight.state)
        restarted = ContextLeaseStore(SQLiteDatabase(self.database.path))
        self.assertEqual((in_flight,), restarted.recovery_leases())
        with self.assertRaises(LeaseFenceRejectedError):
            restarted.finish_atomic_call(
                lease_id=later.lease_id,
                fencing_token=later.fencing_token,
                owner_instance_id=OWNER_B,
                workflow_version=later.workflow_version,
                operation_id=OPERATION_ID,
                occurred_at=NOW + timedelta(minutes=2, seconds=2),
                reason="late outcome rejected pending recovery",
                evidence_reference="evidence:v1:late-outcome",
            )


if __name__ == "__main__":
    unittest.main()
