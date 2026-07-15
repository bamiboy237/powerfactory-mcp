from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    AdmissionStatus,
    ApprovalRequest,
    AuditActorClass,
    AuditEventType,
    AuthorityApprovalRequest,
    ConfigurationKey,
    ContentDigest,
    LiveStateFingerprint,
    MutationStrategy,
    OperationType,
    VersionedName,
    WorkflowRecord,
    WorkflowState,
    WorkflowVersion,
    WorkspaceRevision,
)
from powerfactory_agent.persistence import ApprovalAuthorityStore, SQLiteDatabase, WorkflowStore


WORKFLOW_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
REQUEST_ID = "33333333-3333-4333-8333-333333333333"
PREVIEW_ID = "44444444-4444-4444-8444-444444444444"
AUTHORITY_ID = "55555555-5555-4555-8555-555555555555"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def digest(kind: str, character: str) -> str:
    return f"{kind}:v1:sha256:{character * 64}"


class ApprovalAuthorityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.directory.name) / "authority.sqlite")
        self.workflows = WorkflowStore(self.database)
        self.configuration = ConfigurationKey(digest("configuration-key", "1"))
        self.proposal = ContentDigest(digest("content", "2"))
        self.workflows.record(
            WorkflowRecord(
                WORKFLOW_ID,
                WorkflowState.NEW,
                WorkflowVersion(WORKFLOW_ID, 0),
                VersionedName("area-load-scaling", "v1"),
                self.configuration,
                self.proposal,
                NOW,
                NOW,
            )
        )
        self.workflows.transition(
            workflow_id=WORKFLOW_ID,
            command_name="start_preview",
            idempotency_key="preview-start",
            request_digest=self.proposal,
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 0),
            target_state=WorkflowState.PREVIEWING,
            transition_event_type=AuditEventType.PREVIEW_STARTED,
            actor_class=AuditActorClass.WORKFLOW_SERVICE,
            evidence_reference="evidence:v1:preview-start",
            occurred_at=NOW,
        )
        self.workflows.transition(
            workflow_id=WORKFLOW_ID,
            command_name="record_preview",
            idempotency_key="preview-record",
            request_digest=self.proposal,
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 1),
            target_state=WorkflowState.AWAITING_AUTHORIZATION,
            transition_event_type=AuditEventType.PREVIEW_PERSISTED,
            actor_class=AuditActorClass.WORKFLOW_SERVICE,
            evidence_reference="evidence:v1:preview-record",
            occurred_at=NOW,
        )
        self.store = ApprovalAuthorityStore(self.database, authority_instance_id=AUTHORITY_ID)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def request(self) -> AuthorityApprovalRequest:
        return AuthorityApprovalRequest(
            ApprovalRequest(REQUEST_ID, PREVIEW_ID, self.proposal, NOW, NOW + timedelta(minutes=10), "agent", "client"),
            WORKFLOW_ID,
            self.configuration,
            LiveStateFingerprint(digest("live-state-fingerprint", "3")),
            WorkspaceRevision(WORKSPACE_ID, 1),
            OperationType.AREA_LOAD_SCALING,
            MutationStrategy.DIRECT_LEDGER,
            WorkflowVersion(WORKFLOW_ID, 2),
            (VersionedName("area-load-scaling", "v1"),),
            ContentDigest(digest("content", "4")),
        )

    def test_approved_authorization_is_restart_safe_single_use_and_consumed(self) -> None:
        request = self.store.create_request(self.request())
        authorization = self.store.approve(
            request.approval_request.approval_request_id,
            authenticated_principal_reference="local-principal",
            authorization_expires_at=NOW + timedelta(minutes=5),
            decided_at=NOW + timedelta(seconds=1),
        )
        restarted = ApprovalAuthorityStore(SQLiteDatabase(self.database.path), authority_instance_id=AUTHORITY_ID)
        self.assertEqual(authorization, restarted.authorization(authorization.execution_authorization.execution_id))
        first = restarted.admit(authorization, admitted_at=NOW + timedelta(seconds=2))
        self.assertEqual(AdmissionStatus.ADMITTED, first.status)
        self.assertEqual(first, restarted.admit(authorization, admitted_at=NOW + timedelta(seconds=3)))
        self.assertEqual("consumed", restarted.mark_consumed(authorization.execution_authorization.execution_id, occurred_at=NOW + timedelta(seconds=4)).value)

    def test_expired_or_forged_authorization_never_admits(self) -> None:
        self.store.create_request(self.request())
        authorization = self.store.approve(
            REQUEST_ID,
            authenticated_principal_reference="local-principal",
            authorization_expires_at=NOW + timedelta(seconds=2),
            decided_at=NOW + timedelta(seconds=1),
        )
        expired = self.store.admit(authorization, admitted_at=NOW + timedelta(seconds=3))
        self.assertEqual(AdmissionStatus.EXPIRED, expired.status)

    def test_forged_authorization_never_admits(self) -> None:
        self.store.create_request(self.request())
        valid = self.store.approve(
            REQUEST_ID,
            authenticated_principal_reference="local-principal",
            authorization_expires_at=NOW + timedelta(minutes=5),
            decided_at=NOW + timedelta(seconds=1),
        )
        forged = replace(valid, authority_instance_id="66666666-6666-4666-8666-666666666666")
        self.assertEqual(AdmissionStatus.INVALIDATED, self.store.admit(forged, admitted_at=NOW + timedelta(seconds=2)).status)

    def test_request_rejects_non_waiting_or_changed_workflow_bindings(self) -> None:
        self.workflows.transition(
            workflow_id=WORKFLOW_ID,
            command_name="reject_or_expire_preview",
            idempotency_key="end-preview",
            request_digest=self.proposal,
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 2),
            target_state=WorkflowState.ABANDONED,
            transition_event_type=AuditEventType.MANUAL_DISPOSITION,
            actor_class=AuditActorClass.WORKFLOW_SERVICE,
            evidence_reference="evidence:v1:end-preview",
            occurred_at=NOW,
        )
        with self.assertRaises(ValueError):
            self.store.create_request(self.request())


if __name__ == "__main__":
    unittest.main()
