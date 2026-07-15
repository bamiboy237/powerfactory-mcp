"""Small SQLite foundation with explicit durability and migration settings."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA_VERSION = 8


_MIGRATION_1 = (
    """CREATE TABLE operation_records (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    handler_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    admitted_at_ns INTEGER NOT NULL,
    queue_deadline_at_ns INTEGER NOT NULL,
    client_deadline_at_ns INTEGER NOT NULL,
    engine_health_threshold_ms INTEGER NOT NULL,
    started_at_ns INTEGER,
    finished_at_ns INTEGER,
    result_json TEXT,
    error_json TEXT,
    version INTEGER NOT NULL DEFAULT 0
)""",
    """CREATE INDEX operation_records_state_sequence_idx
    ON operation_records(state, sequence)""",
)


# The model graph remains normalized in SQLite.  NetworkX is deliberately a
# disposable projection, never a second source of truth.
_MIGRATION_2 = (
    """CREATE TABLE graph_contexts (
    model_context_id TEXT PRIMARY KEY,
    configuration_key TEXT NOT NULL,
    extraction_counter INTEGER NOT NULL,
    context_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)""",
    """CREATE TABLE graph_extraction_runs (
    run_id TEXT PRIMARY KEY,
    model_context_id TEXT NOT NULL REFERENCES graph_contexts(model_context_id),
    extraction_counter INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    mode TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(model_context_id, extraction_counter)
)""",
    """CREATE TABLE graph_assets (
    run_id TEXT NOT NULL REFERENCES graph_extraction_runs(run_id) ON DELETE CASCADE,
    product_identity TEXT NOT NULL,
    asset_json TEXT NOT NULL,
    PRIMARY KEY(run_id, product_identity)
)""",
    """CREATE TABLE graph_attributes (
    run_id TEXT NOT NULL REFERENCES graph_extraction_runs(run_id) ON DELETE CASCADE,
    product_identity TEXT NOT NULL,
    attribute_name TEXT NOT NULL,
    attribute_json TEXT NOT NULL,
    PRIMARY KEY(run_id, product_identity, attribute_name)
)""",
    """CREATE TABLE graph_relationships (
    run_id TEXT NOT NULL REFERENCES graph_extraction_runs(run_id) ON DELETE CASCADE,
    relationship_id TEXT NOT NULL,
    relationship_json TEXT NOT NULL,
    PRIMARY KEY(run_id, relationship_id)
)""",
    """CREATE TABLE graph_provenance (
    run_id TEXT NOT NULL REFERENCES graph_extraction_runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    provenance_json TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence)
)""",
    "CREATE INDEX graph_extraction_runs_latest_idx ON graph_extraction_runs(recorded_at DESC)",
    "CREATE INDEX graph_assets_identity_idx ON graph_assets(product_identity)",
)


# Calculation snapshots are immutable durable evidence.  Overlay rows are a
# derived cache/projection and can always be rebuilt from snapshot JSON.
_MIGRATION_3 = (
    """CREATE TABLE calculation_runs (
    run_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    configuration_key TEXT NOT NULL,
    extraction_counter INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    convergence_state TEXT NOT NULL,
    result_snapshot_id TEXT,
    run_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)""",
    """CREATE TABLE calculation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES calculation_runs(run_id),
    context_id TEXT NOT NULL,
    configuration_key TEXT NOT NULL,
    extraction_counter INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)""",
    """CREATE TABLE calculation_comparisons (
    comparison_id TEXT PRIMARY KEY,
    baseline_snapshot_id TEXT NOT NULL REFERENCES calculation_snapshots(snapshot_id),
    candidate_snapshot_id TEXT NOT NULL REFERENCES calculation_snapshots(snapshot_id),
    comparison_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)""",
    """CREATE TABLE calculation_overlays (
    overlay_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES calculation_snapshots(snapshot_id) ON DELETE CASCADE,
    product_identity TEXT NOT NULL,
    overlay_kind TEXT NOT NULL,
    overlay_json TEXT NOT NULL
)""",
    "CREATE INDEX calculation_snapshots_context_idx ON calculation_snapshots(context_id, extraction_counter)",
    "CREATE INDEX calculation_overlays_snapshot_idx ON calculation_overlays(snapshot_id, product_identity)",
)


# Workflow state is the only mutable workflow record. Commands and audit facts
# are immutable evidence, protected by SQLite triggers as well as store APIs.
_MIGRATION_4 = (
    """CREATE TABLE workflow_records (
    workflow_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    workflow_version_counter INTEGER NOT NULL,
    configuration_key TEXT NOT NULL,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE workflow_commands (
    command_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    command_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    expected_version_counter INTEGER NOT NULL,
    resulting_version_counter INTEGER NOT NULL,
    command_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(workflow_id, idempotency_key)
)""",
    """CREATE TABLE workflow_audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    workflow_version_counter INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_json TEXT NOT NULL
)""",
    "CREATE INDEX workflow_commands_workflow_idx ON workflow_commands(workflow_id, recorded_at)",
    "CREATE INDEX workflow_audit_events_workflow_idx ON workflow_audit_events(workflow_id, sequence)",
    """CREATE TRIGGER workflow_commands_immutable_update
    BEFORE UPDATE ON workflow_commands BEGIN
        SELECT RAISE(ABORT, 'workflow command records are append-only');
    END""",
    """CREATE TRIGGER workflow_commands_immutable_delete
    BEFORE DELETE ON workflow_commands BEGIN
        SELECT RAISE(ABORT, 'workflow command records are append-only');
    END""",
    """CREATE TRIGGER workflow_audit_events_immutable_update
    BEFORE UPDATE ON workflow_audit_events BEGIN
        SELECT RAISE(ABORT, 'workflow audit events are append-only');
    END""",
    """CREATE TRIGGER workflow_audit_events_immutable_delete
    BEFORE DELETE ON workflow_audit_events BEGIN
        SELECT RAISE(ABORT, 'workflow audit events are append-only');
    END""",
)


# Authority requests, decisions, and admission results are durable facts.  The
# authorization state is the sole mutable row and every change is accompanied
# by an append-only state event.
_MIGRATION_5 = (
    """CREATE TABLE authority_approval_requests (
    approval_request_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    expires_at TEXT NOT NULL,
    request_json TEXT NOT NULL
)""",
    """CREATE TABLE authority_decisions (
    decision_id TEXT PRIMARY KEY,
    approval_request_id TEXT NOT NULL UNIQUE REFERENCES authority_approval_requests(approval_request_id),
    decision_kind TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decision_json TEXT NOT NULL
)""",
    """CREATE TABLE authority_authorizations (
    execution_id TEXT PRIMARY KEY,
    approval_request_id TEXT NOT NULL UNIQUE REFERENCES authority_approval_requests(approval_request_id),
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    authority_instance_id TEXT NOT NULL,
    state TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE authority_authorization_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    execution_id TEXT NOT NULL REFERENCES authority_authorizations(execution_id),
    state_before TEXT,
    state_after TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    occurred_at TEXT NOT NULL
)""",
    """CREATE TABLE authority_admissions (
    execution_id TEXT PRIMARY KEY REFERENCES authority_authorizations(execution_id),
    admission_json TEXT NOT NULL
)""",
    "CREATE INDEX authority_requests_workflow_idx ON authority_approval_requests(workflow_id)",
    "CREATE INDEX authority_authorizations_workflow_idx ON authority_authorizations(workflow_id, state)",
    """CREATE TRIGGER authority_decisions_immutable_update
    BEFORE UPDATE ON authority_decisions BEGIN
        SELECT RAISE(ABORT, 'authority decisions are append-only');
    END""",
    """CREATE TRIGGER authority_decisions_immutable_delete
    BEFORE DELETE ON authority_decisions BEGIN
        SELECT RAISE(ABORT, 'authority decisions are append-only');
    END""",
    """CREATE TRIGGER authority_authorization_events_immutable_update
    BEFORE UPDATE ON authority_authorization_events BEGIN
        SELECT RAISE(ABORT, 'authority authorization events are append-only');
    END""",
    """CREATE TRIGGER authority_authorization_events_immutable_delete
    BEFORE DELETE ON authority_authorization_events BEGIN
        SELECT RAISE(ABORT, 'authority authorization events are append-only');
    END""",
    """CREATE TRIGGER authority_admissions_immutable_update
    BEFORE UPDATE ON authority_admissions BEGIN
        SELECT RAISE(ABORT, 'authority admissions are append-only');
    END""",
    """CREATE TRIGGER authority_admissions_immutable_delete
    BEFORE DELETE ON authority_admissions BEGIN
        SELECT RAISE(ABORT, 'authority admissions are append-only');
    END""",
)


# A scope's fencing counter deliberately survives lease release.  The current
# lease table holds both live and recovery states; AVAILABLE is represented by
# deleting the current row, never by resetting its counter.
_MIGRATION_6 = (
    """CREATE TABLE context_lease_fence_counters (
    service_scope_digest TEXT NOT NULL,
    configuration_key TEXT NOT NULL,
    last_fencing_token INTEGER NOT NULL,
    PRIMARY KEY(service_scope_digest, configuration_key)
)""",
    """CREATE TABLE context_leases (
    service_scope_digest TEXT NOT NULL,
    configuration_key TEXT NOT NULL,
    lease_id TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    workflow_version_counter INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    owner_instance_id TEXT NOT NULL,
    operation_id TEXT,
    recovery_disposition TEXT,
    lease_json TEXT NOT NULL,
    PRIMARY KEY(service_scope_digest, configuration_key)
)""",
    """CREATE TABLE context_lease_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    lease_id TEXT NOT NULL,
    service_scope_digest TEXT NOT NULL,
    configuration_key TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version_counter INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_json TEXT NOT NULL
)""",
    "CREATE INDEX context_leases_workflow_idx ON context_leases(workflow_id)",
    "CREATE INDEX context_lease_events_scope_idx ON context_lease_events(service_scope_digest, configuration_key, sequence)",
    "CREATE INDEX context_lease_events_lease_idx ON context_lease_events(lease_id, sequence)",
    """CREATE TRIGGER context_lease_events_immutable_update
    BEFORE UPDATE ON context_lease_events BEGIN
        SELECT RAISE(ABORT, 'context lease events are append-only');
    END""",
    """CREATE TRIGGER context_lease_events_immutable_delete
    BEFORE DELETE ON context_lease_events BEGIN
        SELECT RAISE(ABORT, 'context lease events are append-only');
    END""",
)


# Reconciliation facts are append-only.  A classification may be appended more
# than once as later fresh evidence arrives; callers select the newest record.
_MIGRATION_7 = (
    """CREATE TABLE reconciliation_intents (
    intent_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    workflow_version_counter INTEGER NOT NULL,
    execution_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    intent_json TEXT NOT NULL
)""",
    """CREATE TABLE reconciliation_observations (
    observation_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES reconciliation_intents(intent_id),
    operation_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    outcome TEXT NOT NULL,
    observation_json TEXT NOT NULL
)""",
    """CREATE TABLE reconciliation_records (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_id TEXT NOT NULL UNIQUE,
    intent_id TEXT NOT NULL REFERENCES reconciliation_intents(intent_id),
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    workflow_version_counter INTEGER NOT NULL,
    classification TEXT NOT NULL,
    classified_at TEXT NOT NULL,
    record_json TEXT NOT NULL
)""",
    "CREATE INDEX reconciliation_intents_workflow_idx ON reconciliation_intents(workflow_id, created_at)",
    "CREATE INDEX reconciliation_observations_intent_idx ON reconciliation_observations(intent_id, observed_at)",
    "CREATE INDEX reconciliation_records_intent_idx ON reconciliation_records(intent_id, sequence)",
    """CREATE TRIGGER reconciliation_intents_immutable_update
    BEFORE UPDATE ON reconciliation_intents BEGIN
        SELECT RAISE(ABORT, 'reconciliation intents are append-only');
    END""",
    """CREATE TRIGGER reconciliation_intents_immutable_delete
    BEFORE DELETE ON reconciliation_intents BEGIN
        SELECT RAISE(ABORT, 'reconciliation intents are append-only');
    END""",
    """CREATE TRIGGER reconciliation_observations_immutable_update
    BEFORE UPDATE ON reconciliation_observations BEGIN
        SELECT RAISE(ABORT, 'reconciliation observations are append-only');
    END""",
    """CREATE TRIGGER reconciliation_observations_immutable_delete
    BEFORE DELETE ON reconciliation_observations BEGIN
        SELECT RAISE(ABORT, 'reconciliation observations are append-only');
    END""",
    """CREATE TRIGGER reconciliation_records_immutable_update
    BEFORE UPDATE ON reconciliation_records BEGIN
        SELECT RAISE(ABORT, 'reconciliation records are append-only');
    END""",
    """CREATE TRIGGER reconciliation_records_immutable_delete
    BEFORE DELETE ON reconciliation_records BEGIN
        SELECT RAISE(ABORT, 'reconciliation records are append-only');
    END""",
)


# The coordinator's envelope is the replay record for a single atomic
# authorization, workflow, lease, and write-ahead-intent admission.
_MIGRATION_8 = (
    """CREATE TABLE execution_admission_envelopes (
    admission_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflow_records(workflow_id),
    idempotency_key TEXT NOT NULL,
    execution_id TEXT NOT NULL UNIQUE REFERENCES authority_authorizations(execution_id),
    operation_id TEXT NOT NULL UNIQUE,
    intent_id TEXT NOT NULL UNIQUE REFERENCES reconciliation_intents(intent_id),
    request_digest TEXT NOT NULL,
    admitted_at TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    UNIQUE(workflow_id, idempotency_key)
)""",
    "CREATE INDEX execution_admission_envelopes_workflow_idx ON execution_admission_envelopes(workflow_id, admitted_at)",
    """CREATE TRIGGER execution_admission_envelopes_immutable_update
    BEFORE UPDATE ON execution_admission_envelopes BEGIN
        SELECT RAISE(ABORT, 'execution admission envelopes are append-only');
    END""",
    """CREATE TRIGGER execution_admission_envelopes_immutable_delete
    BEFORE DELETE ON execution_admission_envelopes BEGIN
        SELECT RAISE(ABORT, 'execution admission envelopes are append-only');
    END""",
)


class DatabaseVersionError(RuntimeError):
    """The database schema is newer than this process understands."""


class SQLiteDatabase:
    """Open short-lived, thread-safe SQLite connections with fixed pragmas."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise TypeError("busy_timeout_ms must be an integer")
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pragma(self, name: str) -> object:
        if name not in {"foreign_keys", "journal_mode", "busy_timeout", "user_version"}:
            raise ValueError("unsupported pragma")
        with self.connect() as connection:
            row = connection.execute(f"PRAGMA {name}").fetchone()
        return None if row is None else row[0]

    def _migrate(self) -> None:
        with self.transaction(immediate=True) as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise DatabaseVersionError(
                    f"database schema version {current} is newer than supported version {SCHEMA_VERSION}"
                )
            if current < 1:
                for statement in _MIGRATION_1:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
                current = 1
            if current < 2:
                for statement in _MIGRATION_2:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 2")
                current = 2
            if current < 3:
                for statement in _MIGRATION_3:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 3")
                current = 3
            if current < 4:
                for statement in _MIGRATION_4:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 4")
                current = 4
            if current < 5:
                for statement in _MIGRATION_5:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 5")
                current = 5
            if current < 6:
                for statement in _MIGRATION_6:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 6")
                current = 6
            if current < 7:
                for statement in _MIGRATION_7:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 7")
                current = 7
            if current < 8:
                for statement in _MIGRATION_8:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 8")


__all__ = ["DatabaseVersionError", "SCHEMA_VERSION", "SQLiteDatabase"]
