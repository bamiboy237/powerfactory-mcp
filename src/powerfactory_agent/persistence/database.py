"""Small SQLite foundation with explicit durability and migration settings."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA_VERSION = 1


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
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


__all__ = ["DatabaseVersionError", "SCHEMA_VERSION", "SQLiteDatabase"]
