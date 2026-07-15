"""Transactional context leases with durable, per-scope fencing tokens."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
import uuid

from powerfactory_agent.domain.lease import (
    ContextLease,
    LeaseEvent,
    LeaseEventType,
    LeaseMode,
    LeaseState,
)
from powerfactory_agent.domain.values import (
    ConfigurationKey,
    ContentDigest,
    WorkflowVersion,
    require_aware,
    require_uuid4,
)
from powerfactory_agent.serialization import canonical_json, from_json

from .database import SQLiteDatabase


_HELD_STATES = frozenset(
    {LeaseState.HELD_PREVIEW, LeaseState.HELD_EXECUTION, LeaseState.HELD_ROLLBACK}
)


class LeaseNotFoundError(LookupError):
    pass


class LeaseBusyError(RuntimeError):
    """The requested context scope has a holder or requires recovery."""

    def __init__(self, current: ContextLease) -> None:
        super().__init__(
            f"context {current.configuration_key.value} is unavailable: {current.state.value}"
        )
        self.current = current


class LeaseWorkflowVersionConflictError(RuntimeError):
    """Lease admission lost the workflow-version compare-and-swap."""

    def __init__(
        self, workflow_id: str, expected: WorkflowVersion, current: WorkflowVersion
    ) -> None:
        super().__init__(
            f"workflow {workflow_id} version conflict: expected {expected.counter}, "
            f"current {current.counter}"
        )
        self.workflow_id = workflow_id
        self.expected = expected
        self.current = current


class LeaseFenceRejectedError(RuntimeError):
    """A caller is not the current, unexpired holder of a lease envelope."""

    def __init__(self, lease: ContextLease, reason: str) -> None:
        super().__init__(f"lease {lease.lease_id} stale fence rejected: {reason}")
        self.lease = lease
        self.reason = reason


class LeaseStateConflictError(RuntimeError):
    pass


class ContextLeaseStore:
    """SQLite-backed exclusive scope admission with append-only evidence.

    This store never performs a vendor call.  Its only mutable state is the
    current lease row; token counters and event evidence remain durable across
    release, expiry, and process restart.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def lease(
        self, *, service_scope_digest: ContentDigest, configuration_key: ConfigurationKey
    ) -> ContextLease | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT lease_json FROM context_leases
                WHERE service_scope_digest = ? AND configuration_key = ?""",
                (service_scope_digest.value, configuration_key.value),
            ).fetchone()
        return None if row is None else from_json(ContextLease, row["lease_json"])

    def lease_events(
        self, *, service_scope_digest: ContentDigest, configuration_key: ConfigurationKey
    ) -> tuple[LeaseEvent, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT event_json FROM context_lease_events
                WHERE service_scope_digest = ? AND configuration_key = ?
                ORDER BY sequence""",
                (service_scope_digest.value, configuration_key.value),
            ).fetchall()
        return tuple(from_json(LeaseEvent, row["event_json"]) for row in rows)

    def recovery_leases(self) -> tuple[ContextLease, ...]:
        """Return durable expiry/recovery rows after a restart without mutation."""
        values = tuple(
            state.value
            for state in (
                LeaseState.EXPIRED,
                LeaseState.IN_FLIGHT_EXPIRED,
                LeaseState.RECONCILIATION_REQUIRED,
                LeaseState.QUARANTINED,
            )
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""SELECT lease_json FROM context_leases
                WHERE state IN ({', '.join('?' for _ in values)})
                ORDER BY service_scope_digest, configuration_key""",
                values,
            ).fetchall()
        return tuple(from_json(ContextLease, row["lease_json"]) for row in rows)

    def acquire(
        self,
        *,
        mode: LeaseMode,
        service_scope_digest: ContentDigest,
        configuration_key: ConfigurationKey,
        workflow_id: str,
        expected_workflow_version: WorkflowVersion,
        owner_instance_id: str,
        issued_at: datetime,
        expires_at: datetime,
        reason: str,
        evidence_reference: str,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextLease:
        """Exclusively admit a scope and mint its next never-reused token."""
        require_uuid4(workflow_id, "workflow_id")
        require_uuid4(owner_instance_id, "owner_instance_id")
        require_aware(issued_at, "issued_at")
        require_aware(expires_at, "expires_at")
        if expected_workflow_version.scope_id != workflow_id:
            raise ValueError("expected_workflow_version scope must equal workflow_id")

        with self.database.transaction(immediate=True) as connection:
            current = self._current(connection, service_scope_digest, configuration_key)
            if current is not None:
                raise LeaseBusyError(current)
            self._assert_workflow_version(connection, workflow_id, expected_workflow_version)
            token = self._mint_token(connection, service_scope_digest, configuration_key)
            lease = ContextLease(
                lease_id=str(uuid.uuid4()),
                service_scope_digest=service_scope_digest,
                configuration_key=configuration_key,
                workflow_id=workflow_id,
                workflow_version=expected_workflow_version,
                fencing_token=token,
                mode=mode,
                state=_held_state(mode),
                issued_at=issued_at,
                expires_at=expires_at,
                owner_instance_id=owner_instance_id,
            )
            self._append_event(
                connection,
                lease,
                event_type=LeaseEventType.ACQUIRE_ATTEMPTED,
                occurred_at=issued_at,
                state_before=LeaseState.AVAILABLE,
                state_after=lease.state,
                reason=reason,
                evidence_reference=evidence_reference,
                command_id=command_id,
                correlation_id=correlation_id,
            )
            self._write_current(connection, lease)
            self._append_event(
                connection,
                lease,
                event_type=LeaseEventType.ACQUIRED,
                occurred_at=issued_at,
                state_before=LeaseState.AVAILABLE,
                state_after=lease.state,
                reason=reason,
                evidence_reference=evidence_reference,
                command_id=command_id,
                correlation_id=correlation_id,
            )
        return lease

    def release_for_authorization(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        owner_instance_id: str,
        workflow_version: WorkflowVersion,
        occurred_at: datetime,
        reason: str,
        evidence_reference: str,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextLease:
        """Release a preview holder before the workflow waits for approval."""
        return self._release(
            lease_id=lease_id,
            fencing_token=fencing_token,
            owner_instance_id=owner_instance_id,
            workflow_version=workflow_version,
            occurred_at=occurred_at,
            reason=reason,
            evidence_reference=evidence_reference,
            event_type=LeaseEventType.RELEASED_FOR_AUTHORIZATION,
            require_preview=True,
            command_id=command_id,
            correlation_id=correlation_id,
        )

    def release(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        owner_instance_id: str,
        workflow_version: WorkflowVersion,
        occurred_at: datetime,
        reason: str,
        evidence_reference: str,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextLease:
        return self._release(
            lease_id=lease_id,
            fencing_token=fencing_token,
            owner_instance_id=owner_instance_id,
            workflow_version=workflow_version,
            occurred_at=occurred_at,
            reason=reason,
            evidence_reference=evidence_reference,
            event_type=LeaseEventType.RELEASED,
            require_preview=False,
            command_id=command_id,
            correlation_id=correlation_id,
        )

    def start_atomic_call(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        owner_instance_id: str,
        workflow_version: WorkflowVersion,
        operation_id: str,
        occurred_at: datetime,
        reason: str,
        evidence_reference: str,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextLease:
        require_uuid4(operation_id, "operation_id")
        with self.database.transaction(immediate=True) as connection:
            current = self._current_by_id(connection, lease_id)
            stale_reason = self._holder_failure(
                connection, current, fencing_token, owner_instance_id, workflow_version, occurred_at,
                require_no_operation=True,
            )
            if stale_reason is not None:
                self._append_stale_rejection(connection, current, fencing_token, occurred_at, stale_reason,
                                              reason, evidence_reference, command_id, correlation_id)
            else:
                next_lease = replace(current, operation_id=operation_id)
                self._write_current(connection, next_lease)
                self._append_event(connection, next_lease, event_type=LeaseEventType.ATOMIC_CALL_STARTED,
                                   occurred_at=occurred_at, state_before=current.state,
                                   state_after=next_lease.state, reason=reason,
                                   evidence_reference=evidence_reference, command_id=command_id,
                                   operation_id=operation_id, correlation_id=correlation_id)
        if stale_reason is not None:
            raise LeaseFenceRejectedError(current, stale_reason)
        return next_lease

    def finish_atomic_call(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        owner_instance_id: str,
        workflow_version: WorkflowVersion,
        operation_id: str,
        occurred_at: datetime,
        reason: str,
        evidence_reference: str,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ContextLease:
        require_uuid4(operation_id, "operation_id")
        with self.database.transaction(immediate=True) as connection:
            current = self._current_by_id(connection, lease_id)
            stale_reason = self._holder_failure(
                connection, current, fencing_token, owner_instance_id, workflow_version, occurred_at
            )
            if stale_reason is None and current.operation_id != operation_id:
                stale_reason = "operation_id does not match the durable atomic call"
            if stale_reason is not None:
                self._append_stale_rejection(connection, current, fencing_token, occurred_at, stale_reason,
                                              reason, evidence_reference, command_id, correlation_id,
                                              operation_id=operation_id)
            else:
                next_lease = replace(current, operation_id=None)
                self._write_current(connection, next_lease)
                self._append_event(connection, next_lease, event_type=LeaseEventType.ATOMIC_CALL_FINISHED,
                                   occurred_at=occurred_at, state_before=current.state,
                                   state_after=next_lease.state, reason=reason,
                                   evidence_reference=evidence_reference, command_id=command_id,
                                   operation_id=operation_id, correlation_id=correlation_id)
        if stale_reason is not None:
            raise LeaseFenceRejectedError(current, stale_reason)
        return next_lease

    def expire(
        self,
        *,
        service_scope_digest: ContentDigest,
        configuration_key: ConfigurationKey,
        occurred_at: datetime,
        reason: str,
        evidence_reference: str,
        correlation_id: str | None = None,
    ) -> ContextLease:
        """Persist expiry without guessing whether an in-flight call completed."""
        require_aware(occurred_at, "occurred_at")
        with self.database.transaction(immediate=True) as connection:
            current = self._current(connection, service_scope_digest, configuration_key)
            if current is None:
                raise LeaseNotFoundError(configuration_key.value)
            if current.state not in _HELD_STATES:
                raise LeaseStateConflictError(f"lease cannot expire from {current.state.value}")
            if occurred_at < current.expires_at:
                raise LeaseStateConflictError("lease has not expired")
            if current.operation_id is None:
                next_lease = replace(
                    current,
                    state=LeaseState.EXPIRED,
                    recovery_disposition="expired before an atomic call was durably started",
                )
                self._append_event(connection, next_lease, event_type=LeaseEventType.EXPIRED,
                                   occurred_at=occurred_at, state_before=current.state,
                                   state_after=next_lease.state, reason=reason,
                                   evidence_reference=evidence_reference, operation_id=current.operation_id,
                                   correlation_id=correlation_id)
                # A pre-call expiry proves no native effect began. Retain the
                # event, but free the current scope for a newer fencing token.
                connection.execute(
                    "DELETE FROM context_leases WHERE service_scope_digest = ? AND configuration_key = ?",
                    (service_scope_digest.value, configuration_key.value),
                )
                return next_lease
            else:
                next_lease = replace(
                    current,
                    state=LeaseState.IN_FLIGHT_EXPIRED,
                    recovery_disposition="atomic call outcome requires reconciliation",
                )
            self._write_current(connection, next_lease)
            self._append_event(connection, next_lease, event_type=LeaseEventType.EXPIRED,
                               occurred_at=occurred_at, state_before=current.state,
                               state_after=next_lease.state, reason=reason,
                               evidence_reference=evidence_reference, operation_id=current.operation_id,
                               correlation_id=correlation_id)
            if current.operation_id is None:
                # No native effect was admitted, so expiry returns the scope to
                # availability while preserving the expired transition in the
                # append-only event stream and retaining the fencing counter.
                connection.execute(
                    "DELETE FROM context_leases WHERE service_scope_digest = ? AND configuration_key = ?",
                    (current.service_scope_digest.value, current.configuration_key.value),
                )
        return next_lease

    def expire_due(
        self, *, occurred_at: datetime, reason: str, evidence_reference: str
    ) -> tuple[ContextLease, ...]:
        """Expire every due held lease; restart code can call this before admission."""
        require_aware(occurred_at, "occurred_at")
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT lease_json FROM context_leases
                WHERE state IN (?, ?, ?) AND expires_at <= ?
                ORDER BY service_scope_digest, configuration_key""",
                (*[state.value for state in _HELD_STATES], _timestamp(occurred_at)),
            ).fetchall()
        return tuple(
            self.expire(
                service_scope_digest=lease.service_scope_digest,
                configuration_key=lease.configuration_key,
                occurred_at=occurred_at,
                reason=reason,
                evidence_reference=evidence_reference,
            )
            for lease in (from_json(ContextLease, row["lease_json"]) for row in rows)
        )

    def _release(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        owner_instance_id: str,
        workflow_version: WorkflowVersion,
        occurred_at: datetime,
        reason: str,
        evidence_reference: str,
        event_type: LeaseEventType,
        require_preview: bool,
        command_id: str | None,
        correlation_id: str | None,
    ) -> ContextLease:
        with self.database.transaction(immediate=True) as connection:
            current = self._current_by_id(connection, lease_id)
            stale_reason = self._holder_failure(
                connection, current, fencing_token, owner_instance_id, workflow_version, occurred_at,
                require_no_operation=True,
            )
            if stale_reason is None and require_preview and current.mode is not LeaseMode.PREVIEW:
                stale_reason = "only a preview lease may be released for authorization"
            if stale_reason is not None:
                self._append_stale_rejection(connection, current, fencing_token, occurred_at, stale_reason,
                                              reason, evidence_reference, command_id, correlation_id)
            else:
                self._append_event(connection, current, event_type=event_type, occurred_at=occurred_at,
                                   state_before=current.state, state_after=LeaseState.AVAILABLE,
                                   reason=reason, evidence_reference=evidence_reference,
                                   command_id=command_id, correlation_id=correlation_id)
                connection.execute(
                    "DELETE FROM context_leases WHERE service_scope_digest = ? AND configuration_key = ?",
                    (current.service_scope_digest.value, current.configuration_key.value),
                )
        if stale_reason is not None:
            raise LeaseFenceRejectedError(current, stale_reason)
        return current

    @staticmethod
    def _current(
        connection: sqlite3.Connection,
        service_scope_digest: ContentDigest,
        configuration_key: ConfigurationKey,
    ) -> ContextLease | None:
        row = connection.execute(
            """SELECT lease_json FROM context_leases
            WHERE service_scope_digest = ? AND configuration_key = ?""",
            (service_scope_digest.value, configuration_key.value),
        ).fetchone()
        return None if row is None else from_json(ContextLease, row["lease_json"])

    @staticmethod
    def _current_by_id(connection: sqlite3.Connection, lease_id: str) -> ContextLease:
        require_uuid4(lease_id, "lease_id")
        row = connection.execute(
            "SELECT lease_json FROM context_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()
        if row is None:
            raise LeaseNotFoundError(lease_id)
        return from_json(ContextLease, row["lease_json"])

    @staticmethod
    def _assert_workflow_version(
        connection: sqlite3.Connection, workflow_id: str, expected: WorkflowVersion
    ) -> None:
        row = connection.execute(
            "SELECT workflow_version_counter FROM workflow_records WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise LeaseNotFoundError(f"workflow {workflow_id}")
        current = WorkflowVersion(workflow_id, int(row["workflow_version_counter"]))
        if current != expected:
            raise LeaseWorkflowVersionConflictError(workflow_id, expected, current)

    def _holder_failure(
        self,
        connection: sqlite3.Connection,
        current: ContextLease,
        fencing_token: object,
        owner_instance_id: object,
        workflow_version: object,
        occurred_at: object,
        *,
        require_no_operation: bool = False,
    ) -> str | None:
        _require_token(fencing_token)
        if not isinstance(owner_instance_id, str):
            raise TypeError("owner_instance_id must be a string")
        require_uuid4(owner_instance_id, "owner_instance_id")
        if not isinstance(workflow_version, WorkflowVersion):
            raise TypeError("workflow_version must be a WorkflowVersion")
        require_aware(occurred_at, "occurred_at")
        if current.state not in _HELD_STATES:
            return f"lease is {current.state.value}"
        if current.fencing_token != fencing_token:
            return "fencing token is not current"
        if current.owner_instance_id != owner_instance_id:
            return "owner instance is not current"
        if current.workflow_version != workflow_version:
            return "workflow version is not current"
        if occurred_at >= current.expires_at:
            return "lease has expired"
        if require_no_operation and current.operation_id is not None:
            return "an atomic call is in flight"
        try:
            self._assert_workflow_version(connection, current.workflow_id, current.workflow_version)
        except LeaseWorkflowVersionConflictError:
            return "workflow version compare-and-swap no longer matches"
        return None

    @staticmethod
    def _mint_token(
        connection: sqlite3.Connection,
        service_scope_digest: ContentDigest,
        configuration_key: ConfigurationKey,
    ) -> int:
        row = connection.execute(
            """SELECT last_fencing_token FROM context_lease_fence_counters
            WHERE service_scope_digest = ? AND configuration_key = ?""",
            (service_scope_digest.value, configuration_key.value),
        ).fetchone()
        token = 1 if row is None else int(row["last_fencing_token"]) + 1
        connection.execute(
            """INSERT INTO context_lease_fence_counters(
            service_scope_digest, configuration_key, last_fencing_token
            ) VALUES (?, ?, ?)
            ON CONFLICT(service_scope_digest, configuration_key)
            DO UPDATE SET last_fencing_token = excluded.last_fencing_token""",
            (service_scope_digest.value, configuration_key.value, token),
        )
        return token

    @staticmethod
    def _write_current(connection: sqlite3.Connection, lease: ContextLease) -> None:
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
                lease.service_scope_digest.value,
                lease.configuration_key.value,
                lease.lease_id,
                lease.workflow_id,
                lease.workflow_version.counter,
                lease.fencing_token,
                lease.mode.value,
                lease.state.value,
                _timestamp(lease.issued_at),
                _timestamp(lease.expires_at),
                lease.owner_instance_id,
                lease.operation_id,
                lease.recovery_disposition,
                canonical_json(lease),
            ),
        )

    def _append_stale_rejection(
        self,
        connection: sqlite3.Connection,
        lease: ContextLease,
        asserted_token: object,
        occurred_at: datetime,
        stale_reason: str,
        reason: str,
        evidence_reference: str,
        command_id: str | None,
        correlation_id: str | None,
        *,
        operation_id: str | None = None,
    ) -> None:
        # Keep the rejected token in evidence when valid; malformed input cannot
        # create a domain event and is rejected before this method is reached.
        self._append_event(
            connection,
            lease,
            event_type=LeaseEventType.STALE_FENCE_REJECTED,
            occurred_at=occurred_at,
            state_before=lease.state,
            state_after=lease.state,
            reason=f"{reason}: {stale_reason}",
            evidence_reference=evidence_reference,
            command_id=command_id,
            operation_id=operation_id,
            correlation_id=correlation_id,
            fencing_token=int(asserted_token),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        lease: ContextLease,
        *,
        event_type: LeaseEventType,
        occurred_at: datetime,
        state_before: LeaseState,
        state_after: LeaseState,
        reason: str,
        evidence_reference: str,
        command_id: str | None = None,
        operation_id: str | None = None,
        correlation_id: str | None = None,
        fencing_token: int | None = None,
    ) -> LeaseEvent:
        event = LeaseEvent(
            event_id=str(uuid.uuid4()),
            lease_id=lease.lease_id,
            service_scope_digest=lease.service_scope_digest,
            configuration_key=lease.configuration_key,
            workflow_id=lease.workflow_id,
            workflow_version=lease.workflow_version,
            fencing_token=lease.fencing_token if fencing_token is None else fencing_token,
            event_type=event_type,
            occurred_at=occurred_at,
            state_before=state_before,
            state_after=state_after,
            reason=reason,
            evidence_reference=evidence_reference,
            command_id=command_id,
            operation_id=operation_id,
            correlation_id=correlation_id,
        )
        connection.execute(
            """INSERT INTO context_lease_events(
            event_id, lease_id, service_scope_digest, configuration_key, workflow_id,
            workflow_version_counter, fencing_token, event_type, occurred_at, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.lease_id,
                event.service_scope_digest.value,
                event.configuration_key.value,
                event.workflow_id,
                event.workflow_version.counter,
                event.fencing_token,
                event.event_type.value,
                _timestamp(event.occurred_at),
                canonical_json(event),
            ),
        )
        return event


def _held_state(mode: LeaseMode) -> LeaseState:
    return {
        LeaseMode.PREVIEW: LeaseState.HELD_PREVIEW,
        LeaseMode.EXECUTION: LeaseState.HELD_EXECUTION,
        LeaseMode.ROLLBACK: LeaseState.HELD_ROLLBACK,
    }[mode]


def _require_token(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("fencing_token must be a positive integer")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ContextLeaseStore",
    "LeaseBusyError",
    "LeaseFenceRejectedError",
    "LeaseNotFoundError",
    "LeaseStateConflictError",
    "LeaseWorkflowVersionConflictError",
]
