from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    ApprovalRequest,
    AttributeKind,
    AttributeSelector,
    AuditActorClass,
    AuditEventType,
    AuthorityApprovalRequest,
    ConfigurationKey,
    ContentDigest,
    LiveStateFingerprint,
    MutationStrategy,
    OperationType,
    ProductIdentity,
    Quantity,
    VersionedName,
    WorkflowRecord,
    WorkflowState,
    WorkflowVersion,
    WorkspaceRevision,
    WriteAheadIntent,
)
from powerfactory_agent.persistence import (
    ApprovalAuthorityStore,
    ContextLeaseStore,
    ExecutionAdmissionCoordinator,
    ExecutionAdmissionRejectedError,
    ReconciliationStore,
    SQLiteDatabase,
    WorkflowStore,
)


WORKFLOW_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
REQUEST_ID = "33333333-3333-4333-8333-333333333333"
PREVIEW_ID = "44444444-4444-4444-8444-444444444444"
AUTHORITY_ID = "55555555-5555-4555-8555-555555555555"
INTENT_ID = "66666666-6666-4666-8666-666666666666"
OPERATION_ID = "77777777-7777-4777-8777-777777777777"
LEASE_ID = "88888888-8888-4888-8888-888888888888"
PRODUCT_ID = "99999999-9999-4999-8999-999999999999"
LOCATOR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OWNER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CORRELATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def digest(kind: str, character: str) -> str:
    return f"{kind}:v1:sha256:{character * 64}"


class ExecutionAdmissionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.directory.name) / "admission.sqlite")
        self.workflows = WorkflowStore(self.database)
        self.authority = ApprovalAuthorityStore(self.database, authority_instance_id=AUTHORITY_ID)
        self.leases = ContextLeaseStore(self.database)
        self.reconciliation = ReconciliationStore(self.database)
        self.coordinator = ExecutionAdmissionCoordinator(self.database)
        self.scope = ContentDigest(digest("content", "1"))
        self.configuration = ConfigurationKey(digest("configuration-key", "2"))
        self.proposal = ContentDigest(digest("content", "3"))
        self.fingerprint = LiveStateFingerprint(digest("live-state-fingerprint", "4"))
        self.workflows.record(
            WorkflowRecord(
                WORKFLOW_ID, WorkflowState.NEW, WorkflowVersion(WORKFLOW_ID, 0),
                VersionedName("area-load-scaling", "v1"), self.configuration, self.proposal, NOW, NOW,
            )
        )
        self.workflows.transition(
            workflow_id=WORKFLOW_ID, command_name="start_preview", idempotency_key="preview-start",
            request_digest=self.proposal, expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 0),
            target_state=WorkflowState.PREVIEWING, transition_event_type=AuditEventType.PREVIEW_STARTED,
            actor_class=AuditActorClass.WORKFLOW_SERVICE, evidence_reference="evidence:v1:preview", occurred_at=NOW,
        )
        self.workflows.transition(
            workflow_id=WORKFLOW_ID, command_name="record_preview", idempotency_key="preview-record",
            request_digest=self.proposal, expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 1),
            target_state=WorkflowState.AWAITING_AUTHORIZATION,
            transition_event_type=AuditEventType.PREVIEW_PERSISTED,
            actor_class=AuditActorClass.WORKFLOW_SERVICE, evidence_reference="evidence:v1:preview", occurred_at=NOW,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def approved_authorization(self):
        request = self.authority.create_request(
            AuthorityApprovalRequest(
                ApprovalRequest(REQUEST_ID, PREVIEW_ID, self.proposal, NOW, NOW + timedelta(minutes=10), "agent", "client"),
                WORKFLOW_ID, self.configuration, self.fingerprint, WorkspaceRevision(WORKSPACE_ID, 1),
                OperationType.AREA_LOAD_SCALING, MutationStrategy.DIRECT_LEDGER,
                WorkflowVersion(WORKFLOW_ID, 2), (VersionedName("area-load-scaling", "v1"),),
                ContentDigest(digest("content", "5")),
            )
        )
        return self.authority.approve(
            request.approval_request.approval_request_id,
            authenticated_principal_reference="local-principal",
            authorization_expires_at=NOW + timedelta(minutes=5),
            decided_at=NOW + timedelta(seconds=1),
        )

    def intent(self, execution_id: str) -> WriteAheadIntent:
        return WriteAheadIntent(
            intent_id=INTENT_ID, operation_id=OPERATION_ID, workflow_id=WORKFLOW_ID,
            workflow_version=WorkflowVersion(WORKFLOW_ID, 3), idempotency_key="change-1",
            execution_id=execution_id, lease_id=LEASE_ID, fencing_token=1, workspace_id=WORKSPACE_ID,
            workspace_revision=WorkspaceRevision(WORKSPACE_ID, 1), product_identity=ProductIdentity(PRODUCT_ID),
            locator_version_id=LOCATOR_ID,
            attribute=AttributeSelector(AttributeKind.ACTIVE_POWER, VersionedName("attribute-selector", "v1")),
            expected_before=Quantity(Decimal("12"), "MW"), proposed_after=Quantity(Decimal("14"), "MW"),
            configuration_key=self.configuration, live_state_fingerprint=self.fingerprint,
            request_digest=self.proposal, policy_versions=(VersionedName("area-load-scaling", "v1"),),
            owner_instance_id=OWNER_ID, session_id="single-owner-session", correlation_id=CORRELATION_ID,
            attempt_number=1, created_at=NOW + timedelta(seconds=2), intent_digest=ContentDigest(digest("content", "6")),
        )

    def test_atomic_admission_creates_one_replay_safe_submission_envelope(self) -> None:
        authorization = self.approved_authorization()
        intent = self.intent(authorization.execution_authorization.execution_id)
        envelope = self.coordinator.admit_and_begin_write(
            authorization=authorization, intent=intent, service_scope_digest=self.scope,
            owner_instance_id=OWNER_ID, lease_expires_at=NOW + timedelta(minutes=1),
            idempotency_key="admit-1", admitted_at=NOW + timedelta(seconds=2),
            evidence_reference="evidence:v1:atomic-admission",
        )
        self.assertEqual(envelope, self.coordinator.admit_and_begin_write(
            authorization=authorization, intent=intent, service_scope_digest=self.scope,
            owner_instance_id=OWNER_ID, lease_expires_at=NOW + timedelta(minutes=1),
            idempotency_key="admit-1", admitted_at=NOW + timedelta(seconds=3),
            evidence_reference="evidence:v1:atomic-admission",
        ))
        self.assertEqual(WorkflowState.EXECUTING, self.workflows.workflow(WORKFLOW_ID).state)
        self.assertEqual(3, self.workflows.workflow(WORKFLOW_ID).workflow_version.counter)
        self.assertEqual("consuming", self.authority.authorization_state(envelope.execution_id).value)
        lease = self.leases.lease(service_scope_digest=self.scope, configuration_key=self.configuration)
        self.assertIsNotNone(lease)
        self.assertEqual((envelope.lease_id, OPERATION_ID, envelope.fencing_token), (lease.lease_id, lease.operation_id, lease.fencing_token))
        self.assertEqual(intent, self.reconciliation.intent(INTENT_ID))
        self.assertEqual(
            ("requested", "admission_revalidated", "intent_committed"),
            tuple(event.event_type.value for event in self.workflows.audit_events(WORKFLOW_ID)[-3:]),
        )

    def test_rejected_owner_or_fence_rolls_back_every_admission_fact(self) -> None:
        authorization = self.approved_authorization()
        intent = self.intent(authorization.execution_authorization.execution_id)
        for rejected in (
            replace(intent, owner_instance_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            replace(intent, fencing_token=2),
        ):
            with self.subTest(intent=rejected.intent_id, fence=rejected.fencing_token):
                with self.assertRaises(ExecutionAdmissionRejectedError):
                    self.coordinator.admit_and_begin_write(
                        authorization=authorization, intent=rejected, service_scope_digest=self.scope,
                        owner_instance_id=OWNER_ID, lease_expires_at=NOW + timedelta(minutes=1),
                        idempotency_key=f"reject-{rejected.fencing_token}", admitted_at=NOW + timedelta(seconds=2),
                        evidence_reference="evidence:v1:atomic-admission",
                    )
                self.assertEqual(WorkflowState.AWAITING_AUTHORIZATION, self.workflows.workflow(WORKFLOW_ID).state)
                self.assertEqual("issued", self.authority.authorization_state(authorization.execution_authorization.execution_id).value)
                self.assertIsNone(self.leases.lease(service_scope_digest=self.scope, configuration_key=self.configuration))
                with self.assertRaises(LookupError):
                    self.reconciliation.intent(INTENT_ID)


if __name__ == "__main__":
    unittest.main()
