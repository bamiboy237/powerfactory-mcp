from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import unittest

from powerfactory_agent.domain import (
    AuditActorClass,
    AuditEvent,
    AuditEventType,
    ContentDigest,
    IdempotentCommandRecord,
    ConfigurationKey,
    VersionedName,
    WorkflowCommandStatus,
    WorkflowRecord,
    WorkflowState,
    WorkflowVersion,
)


WORKFLOW_ID = "11111111-1111-4111-8111-111111111111"
COMMAND_ID = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
OPERATION_ID = "44444444-4444-4444-8444-444444444444"
CORRELATION_ID = "55555555-5555-4555-8555-555555555555"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def digest(suffix: str) -> ContentDigest:
    return ContentDigest(f"content:v1:sha256:{suffix * 64}")


class WorkflowContractTests(unittest.TestCase):
    def record(self, **overrides: object) -> WorkflowRecord:
        values: dict[str, object] = {
            "workflow_id": WORKFLOW_ID,
            "state": WorkflowState.AWAITING_AUTHORIZATION,
            "workflow_version": WorkflowVersion(WORKFLOW_ID, 5),
            "operation_specification": VersionedName("area-load-scaling", "v1"),
            "configuration_key": ConfigurationKey(f"configuration-key:v1:sha256:{'c' * 64}"),
            "proposal_digest": digest("a"),
            "created_at": NOW,
            "updated_at": NOW + timedelta(seconds=1),
            "latest_command_id": COMMAND_ID,
            "latest_operation_id": OPERATION_ID,
            "recovery_reference": "recovery:v1:none",
        }
        values.update(overrides)
        return WorkflowRecord(**values)  # type: ignore[arg-type]

    def command(self, **overrides: object) -> IdempotentCommandRecord:
        values: dict[str, object] = {
            "command_id": COMMAND_ID,
            "workflow_id": WORKFLOW_ID,
            "command_name": "start_preview",
            "idempotency_key": "preview-request-1",
            "request_digest": digest("a"),
            "expected_workflow_version": WorkflowVersion(WORKFLOW_ID, 4),
            "status": WorkflowCommandStatus.COMPLETED,
            "resulting_workflow_version": WorkflowVersion(WORKFLOW_ID, 5),
            "requested_at": NOW,
            "operation_id": OPERATION_ID,
            "result_digest": digest("b"),
            "completed_at": NOW + timedelta(seconds=1),
        }
        values.update(overrides)
        return IdempotentCommandRecord(**values)  # type: ignore[arg-type]

    def event(self, **overrides: object) -> AuditEvent:
        values: dict[str, object] = {
            "event_id": EVENT_ID,
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WorkflowVersion(WORKFLOW_ID, 5),
            "event_type": AuditEventType.PREVIEW_STARTED,
            "actor_class": AuditActorClass.WORKFLOW_SERVICE,
            "occurred_at": NOW,
            "request_digest": digest("a"),
            "evidence_reference": "evidence:v1:preview-started",
            "command_id": COMMAND_ID,
            "operation_id": OPERATION_ID,
            "correlation_id": CORRELATION_ID,
            "state_before": WorkflowState.NEW,
            "state_after": WorkflowState.PREVIEWING,
            "authorization_reference": "authorization:v1:pending",
            "fencing_token": 1,
        }
        values.update(overrides)
        return AuditEvent(**values)  # type: ignore[arg-type]

    def test_workflow_state_enumerates_all_specification_states(self) -> None:
        self.assertEqual(
            {
                "new",
                "previewing",
                "awaiting_authorization",
                "execution_admission",
                "executing",
                "calculating",
                "verifying",
                "completed",
                "failed_before_effect",
                "reconciliation_required",
                "quarantined",
                "rollback_previewing",
                "awaiting_rollback_authorization",
                "rollback_admission",
                "rolling_back",
                "rollback_verifying",
                "rolled_back",
                "abandoned",
            },
            {state.value for state in WorkflowState},
        )

    def test_command_and_audit_records_are_immutable(self) -> None:
        for record in (self.record(), self.command(), self.event()):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, "workflow_id", WORKFLOW_ID)

    def test_workflow_record_binds_current_state_and_versions(self) -> None:
        self.assertEqual(WorkflowState.AWAITING_AUTHORIZATION, self.record().state)
        with self.assertRaises(ValueError):
            self.record(workflow_version=WorkflowVersion(CORRELATION_ID, 5))
        with self.assertRaises(ValueError):
            self.record(updated_at=NOW - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            self.record(latest_command_id="not-a-uuid")
        with self.assertRaises(ValueError):
            self.record(recovery_reference="x" * 1025)

    def test_command_binds_versions_and_completion_to_one_workflow(self) -> None:
        self.assertEqual(5, self.command().resulting_workflow_version.counter)
        with self.assertRaises(ValueError):
            self.command(expected_workflow_version=WorkflowVersion(CORRELATION_ID, 4))
        with self.assertRaises(ValueError):
            self.command(resulting_workflow_version=WorkflowVersion(WORKFLOW_ID, 3))
        with self.assertRaises(ValueError):
            self.command(completed_at=None)
        with self.assertRaises(ValueError):
            self.command(status=WorkflowCommandStatus.IN_PROGRESS)

    def test_command_rejects_bad_ids_timestamps_and_unbounded_strings(self) -> None:
        with self.assertRaises(ValueError):
            self.command(command_id="not-a-uuid")
        with self.assertRaises(ValueError):
            self.command(requested_at=datetime(2026, 7, 15, 12, 0))
        with self.assertRaises(ValueError):
            self.command(idempotency_key="x" * 257)
        with self.assertRaises(ValueError):
            self.command(completed_at=NOW - timedelta(seconds=1))

    def test_audit_event_binds_scope_and_rejects_unbounded_or_unsafe_fields(self) -> None:
        self.assertEqual(WorkflowState.PREVIEWING, self.event().state_after)
        with self.assertRaises(ValueError):
            self.event(workflow_version=WorkflowVersion(CORRELATION_ID, 5))
        with self.assertRaises(ValueError):
            self.event(correlation_id="not-a-uuid")
        with self.assertRaises(ValueError):
            self.event(evidence_reference="x" * 1025)
        with self.assertRaises(ValueError):
            self.event(state_after=None)
        with self.assertRaises(ValueError):
            self.event(fencing_token=0)
        with self.assertRaises(TypeError):
            self.event(fencing_token=True)


if __name__ == "__main__":
    unittest.main()
