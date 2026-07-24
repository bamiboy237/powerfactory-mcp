"""Private transaction-local writers shared by lease, workflow, and admission stores.

Each helper accepts an existing ``sqlite3.Connection`` and performs only writes
inside the caller's already-open transaction; none open connections or
transactions, and none introduce a general repository or base-store
abstraction. SQL text, column order, and parameter order are preserved verbatim
from the prior per-store implementations so persisted formats, event order,
rollback, and idempotent replay behavior are unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from powerfactory_agent.domain.lease import ContextLease, LeaseEvent
from powerfactory_agent.domain.values import ConfigurationKey, ContentDigest
from powerfactory_agent.domain.workflow import AuditEvent, IdempotentCommandRecord
from powerfactory_agent.serialization import canonical_json


def encode_utc_timestamp(value: datetime) -> str:
    """Encode an aware datetime as canonical UTC text with a trailing Z."""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mint_fencing_token(
    connection: sqlite3.Connection,
    *,
    service_scope_digest: ContentDigest,
    configuration_key: ConfigurationKey,
) -> int:
    """Select and upsert the next per-scope fencing token without reusing it."""

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


def write_lease_row(connection: sqlite3.Connection, lease: ContextLease) -> None:
    """Upsert the single current lease row for a scope and configuration key."""

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
            encode_utc_timestamp(lease.issued_at),
            encode_utc_timestamp(lease.expires_at),
            lease.owner_instance_id,
            lease.operation_id,
            lease.recovery_disposition,
            canonical_json(lease),
        ),
    )


def append_lease_event(connection: sqlite3.Connection, event: LeaseEvent) -> LeaseEvent:
    """Append one immutable lease event row and return the event."""

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
            encode_utc_timestamp(event.occurred_at),
            canonical_json(event),
        ),
    )
    return event


def insert_workflow_command(
    connection: sqlite3.Connection, command: IdempotentCommandRecord
) -> None:
    """Insert one immutable workflow command record row."""

    connection.execute(
        """INSERT INTO workflow_commands(
        command_id, workflow_id, command_name, idempotency_key, request_digest,
        expected_version_counter, resulting_version_counter, command_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            command.command_id,
            command.workflow_id,
            command.command_name,
            command.idempotency_key,
            command.request_digest.value,
            command.expected_workflow_version.counter,
            command.resulting_workflow_version.counter,
            canonical_json(command),
            encode_utc_timestamp(command.requested_at),
        ),
    )


def append_workflow_audit_event(
    connection: sqlite3.Connection, event: AuditEvent
) -> AuditEvent:
    """Append one immutable workflow audit event row and return the event."""

    connection.execute(
        """INSERT INTO workflow_audit_events(
        event_id, workflow_id, workflow_version_counter, event_type, occurred_at, event_json
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            event.event_id,
            event.workflow_id,
            event.workflow_version.counter,
            event.event_type.value,
            encode_utc_timestamp(event.occurred_at),
            canonical_json(event),
        ),
    )
    return event


__all__: list[str] = []