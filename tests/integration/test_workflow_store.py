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


if __name__ == "__main__":
    unittest.main()
