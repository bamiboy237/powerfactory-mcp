"""One-transaction durable admission before any serialized-owner submission."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
import uuid

from powerfactory_agent.domain.admission import ExecutionAdmissionEnvelope
from powerfactory_agent.domain.approval import (
    AdmissionStatus,
    AuthorityAuthorization,
    AuthorizationAdmission,
    AuthorizationState,
)
from powerfactory_agent.domain.lease import ContextLease, LeaseEvent, LeaseEventType, LeaseMode, LeaseState
from powerfactory_agent.domain.reconciliation import WriteAheadIntent
from powerfactory_agent.domain.values import ContentDigest, WorkflowVersion, require_aware, require_text, require_uuid4
from powerfactory_agent.domain.workflow import (
    AuditActorClass,
    AuditEvent,
    AuditEventType,
    IdempotentCommandRecord,
    WorkflowCommandStatus,
    WorkflowRecord,
    WorkflowState,
)
from powerfactory_agent.serialization import canonical_json, from_json

from .database import SQLiteDatabase


class ExecutionAdmissionConflictError(ValueError):
    """A replay key or intent identity is bound to different admitted work."""


class ExecutionAdmissionRejectedError(RuntimeError):
    """A durable authorization, workflow, or lease precondition is unsafe."""


class ExecutionAdmissionCoordinator:
    """Commit all write-admission facts in one SQLite transaction.

    The coordinator deliberately has no gateway dependency.  Its successful
    return is only a durable submission envelope; the serialized owner may
    invoke a native write strictly after this method commits.
    """

    command_name = "admit_execution_and_commit_intent"

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def envelope(self, admission_id: str) -> ExecutionAdmissionEnvelope:
        require_uuid4(admission_id, "admission_id")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT envelope_json FROM execution_admission_envelopes WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
        if row is None:
            raise LookupError(admission_id)
        return from_json(ExecutionAdmissionEnvelope, row["envelope_json"])

    def admit_and_begin_write(
        self,
        *,
        authorization: AuthorityAuthorization,
        intent: WriteAheadIntent,
        service_scope_digest: ContentDigest,
        owner_instance_id: str,
        lease_expires_at: datetime,
        idempotency_key: str,
        admitted_at: datetime,
        evidence_reference: str,
    ) -> ExecutionAdmissionEnvelope:
        """Atomically consume one authorization and create its started lease.

        ``intent`` must already contain the final workflow version and lease ID
        expected by this admission.  This prevents a caller from changing any
        target binding after durable authorization has been issued.
        """
        require_uuid4(owner_instance_id, "owner_instance_id")
        require_aware(admitted_at, "admitted_at")
        require_aware(lease_expires_at, "lease_expires_at")
        require_text(idempotency_key, "idempotency_key", maximum=256)
        require_text(evidence_reference, "evidence_reference", maximum=1024)
        if lease_expires_at <= admitted_at:
            raise ValueError("lease_expires_at must follow admitted_at")

        execution = authorization.execution_authorization
        if intent.workflow_id != execution.workflow_id:
            raise ExecutionAdmissionRejectedError("intent workflow does not match authorization")
        if intent.execution_id != execution.execution_id:
            raise ExecutionAdmissionRejectedError("intent execution does not match authorization")
        if intent.request_digest != execution.proposal_digest:
            raise ExecutionAdmissionRejectedError("intent digest does not match authorization")
        if intent.configuration_key != execution.configuration_key:
            raise ExecutionAdmissionRejectedError("intent configuration does not match authorization")
        if intent.live_state_fingerprint != execution.live_state_fingerprint:
            raise ExecutionAdmissionRejectedError("intent fingerprint does not match authorization")
        if intent.workspace_revision != authorization.workspace_revision:
            raise ExecutionAdmissionRejectedError("intent workspace revision does not match authorization")
        if intent.policy_versions != authorization.policy_versions:
            raise ExecutionAdmissionRejectedError("intent policy versions do not match authorization")
        if intent.owner_instance_id != owner_instance_id:
            raise ExecutionAdmissionRejectedError("intent owner does not match the acquiring owner")
        expected_version = execution.expected_workflow_version
        if intent.workflow_version != WorkflowVersion(intent.workflow_id, expected_version.counter + 1):
            raise ExecutionAdmissionRejectedError("intent must bind the next workflow version")

        with self.database.transaction(immediate=True) as connection:
            replay = connection.execute(
                """SELECT envelope_json, request_digest, execution_id, intent_id
                FROM execution_admission_envelopes WHERE workflow_id = ? AND idempotency_key = ?""",
                (intent.workflow_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                envelope = from_json(ExecutionAdmissionEnvelope, replay["envelope_json"])
                if (
                    replay["request_digest"] != intent.request_digest.value
                    or replay["execution_id"] != execution.execution_id
                    or replay["intent_id"] != intent.intent_id
                ):
                    raise ExecutionAdmissionConflictError("idempotency key is bound to different work")
                return envelope

            self._validate_authorization(connection, authorization, admitted_at)
            previous = self._workflow(connection, intent.workflow_id)
            self._validate_workflow(previous, authorization, intent)
            if self._current_lease(connection, service_scope_digest.value, intent.configuration_key.value) is not None:
                raise ExecutionAdmissionRejectedError("execution context is already leased or requires recovery")
            intent_row = connection.execute(
                "SELECT intent_json FROM reconciliation_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if intent_row is not None:
                raise ExecutionAdmissionConflictError("intent ID is already bound to durable evidence")

            command_id = str(uuid.uuid4())
            next_workflow = replace(
                previous,
                state=WorkflowState.EXECUTING,
                workflow_version=intent.workflow_version,
                updated_at=admitted_at,
                latest_command_id=command_id,
                latest_operation_id=intent.operation_id,
            )
            command = IdempotentCommandRecord(
                command_id=command_id,
                workflow_id=intent.workflow_id,
                command_name=self.command_name,
                idempotency_key=idempotency_key,
                request_digest=intent.request_digest,
                expected_workflow_version=expected_version,
                status=WorkflowCommandStatus.COMPLETED,
                resulting_workflow_version=intent.workflow_version,
                requested_at=admitted_at,
                operation_id=intent.operation_id,
                completed_at=admitted_at,
            )
            self._insert_command(connection, command)
            self._insert_audit(
                connection,
                self._audit(
                    workflow=previous,
                    event_type=AuditEventType.REQUESTED,
                    occurred_at=admitted_at,
                    request_digest=intent.request_digest,
                    evidence_reference=evidence_reference,
                    command_id=command_id,
                    operation_id=intent.operation_id,
                    authorization_reference=execution.execution_id,
                ),
            )
            cursor = connection.execute(
                """UPDATE workflow_records
                SET state = ?, workflow_version_counter = ?, record_json = ?, updated_at = ?
                WHERE workflow_id = ? AND workflow_version_counter = ?""",
                (
                    next_workflow.state.value,
                    next_workflow.workflow_version.counter,
                    canonical_json(next_workflow),
                    _timestamp(admitted_at),
                    intent.workflow_id,
                    expected_version.counter,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionAdmissionRejectedError("workflow compare-and-swap failed")

            lease = self._create_started_lease(
                connection,
                intent=intent,
                service_scope_digest=service_scope_digest,
                owner_instance_id=owner_instance_id,
                lease_expires_at=lease_expires_at,
                occurred_at=admitted_at,
                evidence_reference=evidence_reference,
                command_id=command_id,
            )
            self._insert_intent(connection, intent)
            self._consume_authorization(connection, authorization, admitted_at)
            envelope = ExecutionAdmissionEnvelope(
                admission_id=str(uuid.uuid4()),
                workflow_id=intent.workflow_id,
                workflow_version=intent.workflow_version,
                command_id=command_id,
                operation_id=intent.operation_id,
                execution_id=execution.execution_id,
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token,
                intent_id=intent.intent_id,
                request_digest=intent.request_digest,
                admitted_at=admitted_at,
            )
            connection.execute(
                """INSERT INTO execution_admission_envelopes(
                admission_id, workflow_id, idempotency_key, execution_id, operation_id,
                intent_id, request_digest, admitted_at, envelope_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    envelope.admission_id,
                    envelope.workflow_id,
                    idempotency_key,
                    envelope.execution_id,
                    envelope.operation_id,
                    envelope.intent_id,
                    envelope.request_digest.value,
                    _timestamp(admitted_at),
                    canonical_json(envelope),
                ),
            )
            self._insert_audit(
                connection,
                self._audit(
                    workflow=next_workflow,
                    event_type=AuditEventType.ADMISSION_REVALIDATED,
                    occurred_at=admitted_at,
                    request_digest=intent.request_digest,
                    evidence_reference=evidence_reference,
                    command_id=command_id,
                    operation_id=intent.operation_id,
                    authorization_reference=execution.execution_id,
                    fencing_token=lease.fencing_token,
                    state_before=previous.state,
                    state_after=next_workflow.state,
                ),
            )
            self._insert_audit(
                connection,
                self._audit(
                    workflow=next_workflow,
                    event_type=AuditEventType.INTENT_COMMITTED,
                    occurred_at=admitted_at,
                    request_digest=intent.request_digest,
                    evidence_reference=evidence_reference,
                    command_id=command_id,
                    operation_id=intent.operation_id,
                    authorization_reference=execution.execution_id,
                    fencing_token=lease.fencing_token,
                ),
            )
        return envelope

    @staticmethod
    def _workflow(connection: sqlite3.Connection, workflow_id: str) -> WorkflowRecord:
        row = connection.execute(
            "SELECT record_json FROM workflow_records WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise ExecutionAdmissionRejectedError("workflow does not exist")
        return from_json(WorkflowRecord, row["record_json"])

    @staticmethod
    def _validate_workflow(
        workflow: WorkflowRecord, authorization: AuthorityAuthorization, intent: WriteAheadIntent
    ) -> None:
        execution = authorization.execution_authorization
        if workflow.state is not WorkflowState.AWAITING_AUTHORIZATION:
            raise ExecutionAdmissionRejectedError("workflow is not awaiting authorization")
        if workflow.workflow_version != execution.expected_workflow_version:
            raise ExecutionAdmissionRejectedError("workflow version does not match authorization")
        if workflow.configuration_key != execution.configuration_key:
            raise ExecutionAdmissionRejectedError("workflow configuration does not match authorization")
        if workflow.proposal_digest != intent.request_digest:
            raise ExecutionAdmissionRejectedError("workflow proposal does not match intent")

    @staticmethod
    def _validate_authorization(
        connection: sqlite3.Connection, candidate: AuthorityAuthorization, admitted_at: datetime
    ) -> None:
        execution = candidate.execution_authorization
        row = connection.execute(
            "SELECT state, authorization_json FROM authority_authorizations WHERE execution_id = ?",
            (execution.execution_id,),
        ).fetchone()
        if row is None:
            raise ExecutionAdmissionRejectedError("authorization does not exist")
        if AuthorizationState(row["state"]) is not AuthorizationState.ISSUED:
            raise ExecutionAdmissionRejectedError("authorization is not issued")
        if from_json(AuthorityAuthorization, row["authorization_json"]) != candidate:
            raise ExecutionAdmissionRejectedError("authorization integrity failed")
        if admitted_at >= execution.expires_at:
            raise ExecutionAdmissionRejectedError("authorization has expired")
        if connection.execute(
            "SELECT 1 FROM authority_admissions WHERE execution_id = ?", (execution.execution_id,)
        ).fetchone() is not None:
            raise ExecutionAdmissionRejectedError("authorization was already admitted")

    @staticmethod
    def _current_lease(connection: sqlite3.Connection, scope_digest: str, configuration_key: str) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT 1 FROM context_leases
            WHERE service_scope_digest = ? AND configuration_key = ?""",
            (scope_digest, configuration_key),
        ).fetchone()

    def _create_started_lease(
        self,
        connection: sqlite3.Connection,
        *,
        intent: WriteAheadIntent,
        service_scope_digest: ContentDigest,
        owner_instance_id: str,
        lease_expires_at: datetime,
        occurred_at: datetime,
        evidence_reference: str,
        command_id: str,
    ) -> ContextLease:
        counter = connection.execute(
            """SELECT last_fencing_token FROM context_lease_fence_counters
            WHERE service_scope_digest = ? AND configuration_key = ?""",
            (service_scope_digest.value, intent.configuration_key.value),
        ).fetchone()
        token = 1 if counter is None else int(counter["last_fencing_token"]) + 1
        if intent.fencing_token != token:
            raise ExecutionAdmissionRejectedError(
                "intent fencing token does not match the next durable scope token"
            )
        connection.execute(
            """INSERT INTO context_lease_fence_counters(
            service_scope_digest, configuration_key, last_fencing_token
            ) VALUES (?, ?, ?) ON CONFLICT(service_scope_digest, configuration_key)
            DO UPDATE SET last_fencing_token = excluded.last_fencing_token""",
            (service_scope_digest.value, intent.configuration_key.value, token),
        )
        held = ContextLease(
            lease_id=intent.lease_id,
            service_scope_digest=service_scope_digest,
            configuration_key=intent.configuration_key,
            workflow_id=intent.workflow_id,
            workflow_version=intent.workflow_version,
            fencing_token=token,
            mode=LeaseMode.EXECUTION,
            state=LeaseState.HELD_EXECUTION,
            issued_at=occurred_at,
            expires_at=lease_expires_at,
            owner_instance_id=owner_instance_id,
        )
        self._insert_lease_event(
            connection, held, LeaseEventType.ACQUIRE_ATTEMPTED, occurred_at,
            LeaseState.AVAILABLE, held.state, evidence_reference, command_id,
        )
        self._write_lease(connection, held)
        self._insert_lease_event(
            connection, held, LeaseEventType.ACQUIRED, occurred_at,
            LeaseState.AVAILABLE, held.state, evidence_reference, command_id,
        )
        started = replace(held, operation_id=intent.operation_id)
        self._write_lease(connection, started)
        self._insert_lease_event(
            connection, started, LeaseEventType.ATOMIC_CALL_STARTED, occurred_at,
            held.state, started.state, evidence_reference, command_id,
        )
        return started

    @staticmethod
    def _write_lease(connection: sqlite3.Connection, lease: ContextLease) -> None:
        connection.execute(
            """INSERT INTO context_leases(
            service_scope_digest, configuration_key, lease_id, workflow_id,
            workflow_version_counter, fencing_token, mode, state, issued_at,
            expires_at, owner_instance_id, operation_id, recovery_disposition, lease_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_scope_digest, configuration_key) DO UPDATE SET
                lease_id = excluded.lease_id,
                workflow_id = excluded.workflow_id,
                workflow_version_counter = excluded.workflow_version_counter,
                fencing_token = excluded.fencing_token,
                mode = excluded.mode,
                state = excluded.state,
                issued_at = excluded.issued_at,
                expires_at = excluded.expires_at,
                owner_instance_id = excluded.owner_instance_id,
                operation_id = excluded.operation_id,
                recovery_disposition = excluded.recovery_disposition,
                lease_json = excluded.lease_json""",
            (
                lease.service_scope_digest.value, lease.configuration_key.value, lease.lease_id,
                lease.workflow_id, lease.workflow_version.counter, lease.fencing_token,
                lease.mode.value, lease.state.value, _timestamp(lease.issued_at),
                _timestamp(lease.expires_at), lease.owner_instance_id, lease.operation_id,
                lease.recovery_disposition, canonical_json(lease),
            ),
        )

    @staticmethod
    def _insert_lease_event(
        connection: sqlite3.Connection, lease: ContextLease, event_type: LeaseEventType,
        occurred_at: datetime, state_before: LeaseState, state_after: LeaseState,
        evidence_reference: str, command_id: str,
    ) -> None:
        event = LeaseEvent(
            event_id=str(uuid.uuid4()), lease_id=lease.lease_id,
            service_scope_digest=lease.service_scope_digest, configuration_key=lease.configuration_key,
            workflow_id=lease.workflow_id, workflow_version=lease.workflow_version,
            fencing_token=lease.fencing_token, event_type=event_type, occurred_at=occurred_at,
            state_before=state_before, state_after=state_after, reason="atomic_execution_admission",
            evidence_reference=evidence_reference, command_id=command_id,
            operation_id=lease.operation_id,
        )
        connection.execute(
            """INSERT INTO context_lease_events(
            event_id, lease_id, service_scope_digest, configuration_key, workflow_id,
            workflow_version_counter, fencing_token, event_type, occurred_at, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id, event.lease_id, event.service_scope_digest.value,
                event.configuration_key.value, event.workflow_id, event.workflow_version.counter,
                event.fencing_token, event.event_type.value, _timestamp(event.occurred_at), canonical_json(event),
            ),
        )

    @staticmethod
    def _insert_intent(connection: sqlite3.Connection, intent: WriteAheadIntent) -> None:
        connection.execute(
            """INSERT INTO reconciliation_intents(
            intent_id, operation_id, workflow_id, workflow_version_counter,
            execution_id, lease_id, intent_digest, created_at, intent_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                intent.intent_id, intent.operation_id, intent.workflow_id,
                intent.workflow_version.counter, intent.execution_id, intent.lease_id,
                intent.intent_digest.value, _timestamp(intent.created_at), canonical_json(intent),
            ),
        )

    @staticmethod
    def _consume_authorization(
        connection: sqlite3.Connection, authorization: AuthorityAuthorization, occurred_at: datetime
    ) -> None:
        execution = authorization.execution_authorization
        cursor = connection.execute(
            """UPDATE authority_authorizations SET state = ?, updated_at = ?
            WHERE execution_id = ? AND state = ?""",
            (AuthorizationState.CONSUMING.value, _timestamp(occurred_at), execution.execution_id, AuthorizationState.ISSUED.value),
        )
        if cursor.rowcount != 1:
            raise ExecutionAdmissionRejectedError("authorization changed during admission")
        connection.execute(
            """INSERT INTO authority_authorization_events(
            event_id, execution_id, state_before, state_after, reason_code, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), execution.execution_id, AuthorizationState.ISSUED.value,
                AuthorizationState.CONSUMING.value, "atomic_execution_admission", _timestamp(occurred_at),
            ),
        )
        admission = AuthorizationAdmission(
            execution_id=execution.execution_id, workflow_id=execution.workflow_id,
            status=AdmissionStatus.ADMITTED, reason_code="atomic_execution_admission", recorded_at=occurred_at,
        )
        connection.execute(
            "INSERT INTO authority_admissions(execution_id, admission_json) VALUES (?, ?)",
            (execution.execution_id, canonical_json(admission)),
        )

    @staticmethod
    def _insert_command(connection: sqlite3.Connection, command: IdempotentCommandRecord) -> None:
        connection.execute(
            """INSERT INTO workflow_commands(
            command_id, workflow_id, command_name, idempotency_key, request_digest,
            expected_version_counter, resulting_version_counter, command_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                command.command_id, command.workflow_id, command.command_name,
                command.idempotency_key, command.request_digest.value,
                command.expected_workflow_version.counter, command.resulting_workflow_version.counter,
                canonical_json(command), _timestamp(command.requested_at),
            ),
        )

    @staticmethod
    def _audit(
        *, workflow: WorkflowRecord, event_type: AuditEventType, occurred_at: datetime,
        request_digest: ContentDigest, evidence_reference: str, command_id: str,
        operation_id: str, authorization_reference: str, fencing_token: int | None = None,
        state_before: WorkflowState | None = None, state_after: WorkflowState | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            event_id=str(uuid.uuid4()), workflow_id=workflow.workflow_id,
            workflow_version=workflow.workflow_version, event_type=event_type,
            actor_class=AuditActorClass.WORKFLOW_SERVICE, occurred_at=occurred_at,
            request_digest=request_digest, evidence_reference=evidence_reference,
            command_id=command_id, operation_id=operation_id,
            authorization_reference=authorization_reference, fencing_token=fencing_token,
            state_before=state_before, state_after=state_after,
        )

    @staticmethod
    def _insert_audit(connection: sqlite3.Connection, event: AuditEvent) -> None:
        connection.execute(
            """INSERT INTO workflow_audit_events(
            event_id, workflow_id, workflow_version_counter, event_type, occurred_at, event_json
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event.event_id, event.workflow_id, event.workflow_version.counter,
                event.event_type.value, _timestamp(event.occurred_at), canonical_json(event),
            ),
        )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ExecutionAdmissionConflictError",
    "ExecutionAdmissionCoordinator",
    "ExecutionAdmissionRejectedError",
]
