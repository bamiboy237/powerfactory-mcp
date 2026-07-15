"""Small SQLite foundation with explicit durability and migration settings."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA_VERSION = 4


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


__all__ = ["DatabaseVersionError", "SCHEMA_VERSION", "SQLiteDatabase"]
