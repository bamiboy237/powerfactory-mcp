from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    AuditActorClass,
    AuditEventType,
    ConfigurationKey,
    ContentDigest,
    VersionedName,
    WorkflowRecord,
    WorkflowState,
    WorkflowVersion,
)
from powerfactory_agent.persistence import (
    SQLiteDatabase,
    WorkflowIdempotencyConflictError,
    WorkflowStore,
    WorkflowVersionConflictError,
)


WORKFLOW_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def digest(character: str) -> ContentDigest:
    return ContentDigest(f"content:v1:sha256:{character * 64}")


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.directory.name) / "workflow.sqlite")
        self.store = WorkflowStore(self.database)
        self.workflow = WorkflowRecord(
            WORKFLOW_ID,
            WorkflowState.NEW,
            WorkflowVersion(WORKFLOW_ID, 0),
            VersionedName("area-load-scaling", "v1"),
            ConfigurationKey("configuration-key:v1:sha256:" + "1" * 64),
            digest("2"),
            NOW,
            NOW,
        )
        self.store.record(self.workflow)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _transition(self, **overrides: object):
        values: dict[str, object] = {
            "workflow_id": WORKFLOW_ID,
            "command_name": "start_preview",
            "idempotency_key": "preview-1",
            "request_digest": digest("3"),
            "expected_workflow_version": WorkflowVersion(WORKFLOW_ID, 0),
            "target_state": WorkflowState.PREVIEWING,
            "transition_event_type": AuditEventType.PREVIEW_STARTED,
            "actor_class": AuditActorClass.WORKFLOW_SERVICE,
            "evidence_reference": "evidence:v1:preview",
            "occurred_at": NOW,
        }
        values.update(overrides)
        return self.store.transition(**values)

    def test_transition_is_restart_safe_and_appends_ordered_audit_pair(self) -> None:
        command = self._transition()
        restarted = WorkflowStore(SQLiteDatabase(self.database.path))
        self.assertEqual(command, restarted.command(command.command_id))
        self.assertEqual(WorkflowState.PREVIEWING, restarted.workflow(WORKFLOW_ID).state)
        events = restarted.audit_events(WORKFLOW_ID)
        self.assertEqual(("requested", "preview_started"), tuple(item.event_type.value for item in events))
        self.assertEqual((0, 1), tuple(item.workflow_version.counter for item in events))

    def test_exact_replay_does_not_increment_or_append_again(self) -> None:
        command = self._transition()
        self.assertEqual(command, self._transition(evidence_reference="evidence:v1:ignored-on-replay"))
        self.assertEqual(1, self.store.workflow(WORKFLOW_ID).workflow_version.counter)
        self.assertEqual(2, len(self.store.audit_events(WORKFLOW_ID)))

    def test_conflict_stale_version_and_illegal_state_jump_fail_closed(self) -> None:
        self._transition()
        with self.assertRaises(WorkflowIdempotencyConflictError):
            self._transition(request_digest=digest("4"))
        with self.assertRaises(WorkflowVersionConflictError):
            self._transition(
                command_name="record_preview",
                idempotency_key="preview-2",
                expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 0),
                target_state=WorkflowState.AWAITING_AUTHORIZATION,
                transition_event_type=AuditEventType.PREVIEW_PERSISTED,
            )
        with self.assertRaises(ValueError):
            self._transition(
                command_name="complete",
                idempotency_key="illegal",
                expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 1),
                target_state=WorkflowState.COMPLETED,
                transition_event_type=AuditEventType.VERIFIED,
            )
        self.assertEqual(WorkflowState.PREVIEWING, self.store.workflow(WORKFLOW_ID).state)

    def _advance_to_executing(self) -> None:
        self._transition()
        self._transition(
            command_name="record_preview",
            idempotency_key="preview-2",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 1),
            target_state=WorkflowState.AWAITING_AUTHORIZATION,
            transition_event_type=AuditEventType.PREVIEW_PERSISTED,
        )
        self._transition(
            command_name="admit_execution",
            idempotency_key="admit-1",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 2),
            target_state=WorkflowState.EXECUTION_ADMISSION,
            transition_event_type=AuditEventType.ADMISSION_REVALIDATED,
        )
        self._transition(
            command_name="start_execution",
            idempotency_key="execute-1",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 3),
            target_state=WorkflowState.EXECUTING,
            transition_event_type=AuditEventType.SUBMITTED,
        )

    def test_recovery_can_close_a_proven_no_effect_without_replaying_execution(self) -> None:
        self._advance_to_executing()
        recovery = self._transition(
            command_name="require_reconciliation",
            idempotency_key="recover-1",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 4),
            target_state=WorkflowState.RECONCILIATION_REQUIRED,
            transition_event_type=AuditEventType.RECOVERY_STARTED,
            actor_class=AuditActorClass.RECOVERY_SERVICE,
            evidence_reference="evidence:v1:uncertain-owner-outcome",
            recovery_reference="recovery:v1:intent-observation",
        )
        self.assertEqual(WorkflowState.RECONCILIATION_REQUIRED, self.store.workflow(WORKFLOW_ID).state)
        self.assertEqual(5, recovery.resulting_workflow_version.counter)

        recovered = self._transition(
            command_name="recover_no_effect",
            idempotency_key="recover-2",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 5),
            target_state=WorkflowState.FAILED_BEFORE_EFFECT,
            transition_event_type=AuditEventType.RECONCILED,
            actor_class=AuditActorClass.RECOVERY_SERVICE,
            evidence_reference="evidence:v1:before-classification",
            recovery_reference="recovery:v1:before-confirmed",
        )
        self.assertEqual(WorkflowState.FAILED_BEFORE_EFFECT, self.store.workflow(WORKFLOW_ID).state)
        self.assertEqual(6, recovered.resulting_workflow_version.counter)
        self.assertEqual(recovered, self._transition(
            command_name="recover_no_effect",
            idempotency_key="recover-2",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 6),
            target_state=WorkflowState.FAILED_BEFORE_EFFECT,
            transition_event_type=AuditEventType.RECONCILED,
            actor_class=AuditActorClass.RECOVERY_SERVICE,
            evidence_reference="evidence:v1:ignored-on-replay",
            recovery_reference="recovery:v1:ignored-on-replay",
        ))
        self.assertEqual(12, len(self.store.audit_events(WORKFLOW_ID)))

    def test_reconciliation_can_quarantine_and_prevents_no_effect_recovery(self) -> None:
        self._advance_to_executing()
        self._transition(
            command_name="require_reconciliation",
            idempotency_key="recover-1",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 4),
            target_state=WorkflowState.RECONCILIATION_REQUIRED,
            transition_event_type=AuditEventType.RECOVERY_STARTED,
            actor_class=AuditActorClass.RECOVERY_SERVICE,
            evidence_reference="evidence:v1:uncertain-owner-outcome",
        )
        quarantined = self._transition(
            command_name="quarantine",
            idempotency_key="quarantine-1",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 5),
            target_state=WorkflowState.QUARANTINED,
            transition_event_type=AuditEventType.QUARANTINED,
            actor_class=AuditActorClass.RECOVERY_SERVICE,
            evidence_reference="evidence:v1:unavailable-classification",
            recovery_reference="quarantine:v1:reconciliation-unavailable",
        )
        self.assertEqual(WorkflowState.QUARANTINED, self.store.workflow(WORKFLOW_ID).state)
        self.assertEqual(6, quarantined.resulting_workflow_version.counter)
        self.assertEqual(quarantined, self._transition(
            command_name="quarantine",
            idempotency_key="quarantine-1",
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 6),
            target_state=WorkflowState.QUARANTINED,
            transition_event_type=AuditEventType.QUARANTINED,
            actor_class=AuditActorClass.RECOVERY_SERVICE,
            evidence_reference="evidence:v1:ignored-on-replay",
        ))
        with self.assertRaises(ValueError):
            self._transition(
                command_name="recover_no_effect",
                idempotency_key="recover-after-quarantine",
                expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 6),
                target_state=WorkflowState.FAILED_BEFORE_EFFECT,
                transition_event_type=AuditEventType.RECONCILED,
                actor_class=AuditActorClass.RECOVERY_SERVICE,
                evidence_reference="evidence:v1:forbidden-after-quarantine",
            )
        self.assertEqual(WorkflowState.QUARANTINED, self.store.workflow(WORKFLOW_ID).state)


if __name__ == "__main__":
    unittest.main()
