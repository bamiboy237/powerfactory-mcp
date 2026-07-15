"""Durable workflow state, idempotency, and append-only audit evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
import uuid

from powerfactory_agent.domain.workflow import (
    AuditActorClass,
    AuditEvent,
    AuditEventType,
    IdempotentCommandRecord,
    WorkflowCommandStatus,
    WorkflowRecord,
    WorkflowState,
)
from powerfactory_agent.domain.values import ContentDigest, WorkflowVersion, require_aware
from powerfactory_agent.serialization import canonical_json, from_json

from .database import SQLiteDatabase


_LEGAL_TRANSITIONS: dict[str, tuple[WorkflowState, WorkflowState]] = {
    "start_preview": (WorkflowState.NEW, WorkflowState.PREVIEWING),
    "record_preview": (WorkflowState.PREVIEWING, WorkflowState.AWAITING_AUTHORIZATION),
    "reject_or_expire_preview": (WorkflowState.AWAITING_AUTHORIZATION, WorkflowState.ABANDONED),
    "admit_execution": (WorkflowState.AWAITING_AUTHORIZATION, WorkflowState.EXECUTION_ADMISSION),
    "start_execution": (WorkflowState.EXECUTION_ADMISSION, WorkflowState.EXECUTING),
    "start_calculation": (WorkflowState.EXECUTING, WorkflowState.CALCULATING),
    "start_verification": (WorkflowState.CALCULATING, WorkflowState.VERIFYING),
    "complete": (WorkflowState.VERIFYING, WorkflowState.COMPLETED),
    "fail_before_effect": (WorkflowState.EXECUTING, WorkflowState.FAILED_BEFORE_EFFECT),
    "require_reconciliation": (WorkflowState.EXECUTING, WorkflowState.RECONCILIATION_REQUIRED),
    "quarantine": (WorkflowState.RECONCILIATION_REQUIRED, WorkflowState.QUARANTINED),
    "start_rollback_preview": (WorkflowState.COMPLETED, WorkflowState.ROLLBACK_PREVIEWING),
    "record_rollback_preview": (
        WorkflowState.ROLLBACK_PREVIEWING,
        WorkflowState.AWAITING_ROLLBACK_AUTHORIZATION,
    ),
    "admit_rollback": (WorkflowState.AWAITING_ROLLBACK_AUTHORIZATION, WorkflowState.ROLLBACK_ADMISSION),
    "start_rollback": (WorkflowState.ROLLBACK_ADMISSION, WorkflowState.ROLLING_BACK),
    "start_rollback_verification": (WorkflowState.ROLLING_BACK, WorkflowState.ROLLBACK_VERIFYING),
    "complete_rollback": (WorkflowState.ROLLBACK_VERIFYING, WorkflowState.ROLLED_BACK),
}


class WorkflowNotFoundError(LookupError):
    pass


class WorkflowAlreadyExistsError(ValueError):
    pass


class WorkflowIdempotencyConflictError(ValueError):
    """An idempotency key was already bound to a different named command."""

    def __init__(self, workflow_id: str, idempotency_key: str) -> None:
        super().__init__(
            f"workflow {workflow_id} idempotency key {idempotency_key!r} is bound to another request"
        )
        self.workflow_id = workflow_id
        self.idempotency_key = idempotency_key


class WorkflowVersionConflictError(RuntimeError):
    """A workflow transition lost its expected-version compare-and-swap."""

    def __init__(
        self,
        workflow_id: str,
        expected_workflow_version: WorkflowVersion,
        current_workflow_version: WorkflowVersion,
    ) -> None:
        super().__init__(
            f"workflow {workflow_id} version conflict: expected "
            f"{expected_workflow_version.counter}, current {current_workflow_version.counter}"
        )
        self.workflow_id = workflow_id
        self.expected_workflow_version = expected_workflow_version
        self.current_workflow_version = current_workflow_version


class WorkflowStore:
    """SQLite-backed workflow records with atomic CAS and durable audit order.

    This class deliberately knows nothing about authorization, leasing, gateway
    ownership, or PowerFactory. A successful transition only records intent;
    callers may invoke an external dependency after this transaction commits.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def record(self, workflow: WorkflowRecord) -> WorkflowRecord:
        """Persist a new immutable workflow identity and its initial state."""
        try:
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    """INSERT INTO workflow_records(
                    workflow_id, state, workflow_version_counter, configuration_key, record_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        workflow.workflow_id,
                        workflow.state.value,
                        workflow.workflow_version.counter,
                        workflow.configuration_key.value,
                        canonical_json(workflow),
                        _timestamp(workflow.updated_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowAlreadyExistsError(workflow.workflow_id) from exc
        return workflow

    def workflow(self, workflow_id: str) -> WorkflowRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM workflow_records WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFoundError(workflow_id)
        return from_json(WorkflowRecord, row["record_json"])

    def command(self, command_id: str) -> IdempotentCommandRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT command_json FROM workflow_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFoundError(command_id)
        return from_json(IdempotentCommandRecord, row["command_json"])

    def command_for_key(self, workflow_id: str, idempotency_key: str) -> IdempotentCommandRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT command_json FROM workflow_commands
                WHERE workflow_id = ? AND idempotency_key = ?""",
                (workflow_id, idempotency_key),
            ).fetchone()
        return None if row is None else from_json(IdempotentCommandRecord, row["command_json"])

    def append_audit(self, event: AuditEvent) -> AuditEvent:
        """Append a standalone durable fact without changing workflow state."""
        with self.database.transaction(immediate=True) as connection:
            record = self._workflow_row(connection, event.workflow_id)
            current = WorkflowVersion(event.workflow_id, int(record["workflow_version_counter"]))
            if event.workflow_version.counter > current.counter:
                raise WorkflowVersionConflictError(event.workflow_id, event.workflow_version, current)
            self._insert_audit(connection, event)
        return event

    def audit_events(self, workflow_id: str) -> tuple[AuditEvent, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT event_json FROM workflow_audit_events
                WHERE workflow_id = ? ORDER BY sequence""",
                (workflow_id,),
            ).fetchall()
        return tuple(from_json(AuditEvent, row["event_json"]) for row in rows)

    def transition(
        self,
        *,
        workflow_id: str,
        command_name: str,
        idempotency_key: str,
        request_digest: ContentDigest,
        expected_workflow_version: WorkflowVersion,
        target_state: WorkflowState,
        transition_event_type: AuditEventType,
        actor_class: AuditActorClass,
        evidence_reference: str,
        occurred_at: datetime,
        result_digest: ContentDigest | None = None,
        operation_id: str | None = None,
        recovery_reference: str | None = None,
        correlation_id: str | None = None,
        authorization_reference: str | None = None,
        fencing_token: int | None = None,
    ) -> IdempotentCommandRecord:
        """Apply one named transition or return the exact durable replay result.

        A new command writes ``requested`` then the state-changing audit event
        within the same immediate SQLite transaction as the versioned workflow
        row and immutable command record.
        """
        require_aware(occurred_at, "occurred_at")
        if expected_workflow_version.scope_id != workflow_id:
            raise ValueError("expected_workflow_version scope must equal workflow_id")
        if transition_event_type is AuditEventType.REQUESTED:
            raise ValueError("transition_event_type cannot be requested")
        transition = _LEGAL_TRANSITIONS.get(command_name)
        if transition is None:
            raise ValueError(f"unsupported workflow command: {command_name}")
        if not command_name or len(command_name) > 128:
            raise ValueError("command_name must be nonempty and at most 128 characters")
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must be nonempty and at most 256 characters")

        with self.database.transaction(immediate=True) as connection:
            replay_row = connection.execute(
                """SELECT command_json FROM workflow_commands
                WHERE workflow_id = ? AND idempotency_key = ?""",
                (workflow_id, idempotency_key),
            ).fetchone()
            if replay_row is not None:
                replay = from_json(IdempotentCommandRecord, replay_row["command_json"])
                if replay.command_name != command_name or replay.request_digest != request_digest:
                    raise WorkflowIdempotencyConflictError(workflow_id, idempotency_key)
                return replay

            row = self._workflow_row(connection, workflow_id)
            previous = from_json(WorkflowRecord, row["record_json"])
            if previous.workflow_version != expected_workflow_version:
                raise WorkflowVersionConflictError(
                    workflow_id, expected_workflow_version, previous.workflow_version
                )
            if transition != (previous.state, target_state):
                raise ValueError(
                    f"workflow command {command_name} cannot transition "
                    f"{previous.state.value} to {target_state.value}"
                )

            command_id = str(uuid.uuid4())
            next_version = WorkflowVersion(workflow_id, previous.workflow_version.counter + 1)
            next_workflow = replace(
                previous,
                state=target_state,
                workflow_version=next_version,
                updated_at=occurred_at,
                latest_command_id=command_id,
                latest_operation_id=operation_id if operation_id is not None else previous.latest_operation_id,
                recovery_reference=(
                    recovery_reference if recovery_reference is not None else previous.recovery_reference
                ),
            )
            command = IdempotentCommandRecord(
                command_id=command_id,
                workflow_id=workflow_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                expected_workflow_version=expected_workflow_version,
                status=WorkflowCommandStatus.COMPLETED,
                resulting_workflow_version=next_version,
                requested_at=occurred_at,
                operation_id=operation_id,
                result_digest=result_digest,
                completed_at=occurred_at,
            )
            requested = AuditEvent(
                event_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                workflow_version=previous.workflow_version,
                event_type=AuditEventType.REQUESTED,
                actor_class=actor_class,
                occurred_at=occurred_at,
                request_digest=request_digest,
                evidence_reference=evidence_reference,
                command_id=command_id,
                operation_id=operation_id,
                correlation_id=correlation_id,
                authorization_reference=authorization_reference,
                fencing_token=fencing_token,
            )
            transitioned = AuditEvent(
                event_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                workflow_version=next_version,
                event_type=transition_event_type,
                actor_class=actor_class,
                occurred_at=occurred_at,
                request_digest=request_digest,
                evidence_reference=evidence_reference,
                command_id=command_id,
                operation_id=operation_id,
                correlation_id=correlation_id,
                state_before=previous.state,
                state_after=target_state,
                authorization_reference=authorization_reference,
                fencing_token=fencing_token,
            )
            connection.execute(
                """INSERT INTO workflow_commands(
                command_id, workflow_id, command_name, idempotency_key, request_digest,
                expected_version_counter, resulting_version_counter, command_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    command.command_id,
                    workflow_id,
                    command_name,
                    idempotency_key,
                    request_digest.value,
                    expected_workflow_version.counter,
                    next_version.counter,
                    canonical_json(command),
                    _timestamp(occurred_at),
                ),
            )
            cursor = connection.execute(
                """UPDATE workflow_records SET state = ?, workflow_version_counter = ?, record_json = ?, updated_at = ?
                WHERE workflow_id = ? AND workflow_version_counter = ?""",
                (
                    target_state.value,
                    next_version.counter,
                    canonical_json(next_workflow),
                    _timestamp(occurred_at),
                    workflow_id,
                    expected_workflow_version.counter,
                ),
            )
            if cursor.rowcount != 1:
                # An IMMEDIATE transaction makes this unreachable under normal use,
                # but retaining the guard keeps the CAS invariant explicit.
                current_row = self._workflow_row(connection, workflow_id)
                current = WorkflowVersion(workflow_id, int(current_row["workflow_version_counter"]))
                raise WorkflowVersionConflictError(workflow_id, expected_workflow_version, current)
            self._insert_audit(connection, requested)
            self._insert_audit(connection, transitioned)
        return command

    @staticmethod
    def _workflow_row(connection: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT workflow_version_counter, record_json FROM workflow_records WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise WorkflowNotFoundError(workflow_id)
        return row

    @staticmethod
    def _insert_audit(connection: sqlite3.Connection, event: AuditEvent) -> None:
        connection.execute(
            """INSERT INTO workflow_audit_events(
            event_id, workflow_id, workflow_version_counter, event_type, occurred_at, event_json
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.workflow_id,
                event.workflow_version.counter,
                event.event_type.value,
                _timestamp(event.occurred_at),
                canonical_json(event),
            ),
        )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "WorkflowAlreadyExistsError",
    "WorkflowIdempotencyConflictError",
    "WorkflowNotFoundError",
    "WorkflowStore",
    "WorkflowVersionConflictError",
]
