"""Durable, compare-and-swap operation state for the serialized gateway owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
import sqlite3
import time
import uuid
from typing import Iterable

from powerfactory_agent.serialization import MAX_SERIALIZED_BYTES, canonical_json

from .database import SQLiteDatabase


_HANDLER_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_IDEMPOTENCY_KEY_BYTES = 512


class OperationState(str, Enum):
    QUEUED = "QUEUED"
    CANCELLED_BEFORE_START = "CANCELLED_BEFORE_START"
    IN_FLIGHT = "IN_FLIGHT"
    CLIENT_TIMED_OUT = "CLIENT_TIMED_OUT"
    COMPLETED = "COMPLETED"
    COMPLETED_AFTER_CLIENT_TIMEOUT = "COMPLETED_AFTER_CLIENT_TIMEOUT"
    FAILED = "FAILED"
    ENGINE_UNRESPONSIVE = "ENGINE_UNRESPONSIVE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


TERMINAL_STATES = frozenset(
    {
        OperationState.CANCELLED_BEFORE_START,
        OperationState.COMPLETED,
        OperationState.COMPLETED_AFTER_CLIENT_TIMEOUT,
        OperationState.FAILED,
        OperationState.RECONCILIATION_REQUIRED,
    }
)


@dataclass(frozen=True, slots=True)
class OperationRecord:
    sequence: int
    operation_id: str
    idempotency_key: str
    handler_name: str
    payload: object
    state: OperationState
    admitted_at_ns: int
    queue_deadline_at_ns: int
    client_deadline_at_ns: int
    engine_health_threshold_ms: int
    started_at_ns: int | None
    finished_at_ns: int | None
    result: object | None
    error: object | None
    version: int

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class OperationNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class InvalidOperationTransitionError(RuntimeError):
    pass


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def _decode_json(payload: str | None) -> object | None:
    if payload is None:
        return None
    return json.loads(
        payload,
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {token}")),
    )


class OperationStore:
    """SQLite-backed records whose state transitions are atomic and versioned."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        maximum_json_bytes: int = MAX_SERIALIZED_BYTES,
    ) -> None:
        if isinstance(maximum_json_bytes, bool) or not isinstance(maximum_json_bytes, int):
            raise TypeError("maximum_json_bytes must be an integer")
        if maximum_json_bytes < 1:
            raise ValueError("maximum_json_bytes must be positive")
        self.database = database
        self.maximum_json_bytes = maximum_json_bytes

    def admit(
        self,
        *,
        handler_name: str,
        payload: object,
        idempotency_key: str,
        queue_deadline_ms: int,
        client_response_deadline_ms: int,
        engine_health_threshold_ms: int,
        now_ns: int | None = None,
    ) -> tuple[OperationRecord, bool]:
        self._validate_identity(handler_name, idempotency_key)
        for name, value in {
            "queue_deadline_ms": queue_deadline_ms,
            "client_response_deadline_ms": client_response_deadline_ms,
            "engine_health_threshold_ms": engine_health_threshold_ms,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        payload_json = canonical_json(payload, maximum_bytes=self.maximum_json_bytes)
        admitted_at_ns = time.time_ns() if now_ns is None else now_ns
        operation_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            existing_row = connection.execute(
                "SELECT * FROM operation_records WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_row is not None:
                existing = self._record(existing_row)
                if existing.handler_name != handler_name or canonical_json(
                    existing.payload, maximum_bytes=self.maximum_json_bytes
                ) != payload_json:
                    raise IdempotencyConflictError(
                        "idempotency key is already bound to a different named request"
                    )
                return existing, False
            connection.execute(
                """
                INSERT INTO operation_records (
                    operation_id, idempotency_key, handler_name, payload_json, state,
                    admitted_at_ns, queue_deadline_at_ns, client_deadline_at_ns,
                    engine_health_threshold_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    idempotency_key,
                    handler_name,
                    payload_json,
                    OperationState.QUEUED.value,
                    admitted_at_ns,
                    admitted_at_ns + queue_deadline_ms * 1_000_000,
                    admitted_at_ns + client_response_deadline_ms * 1_000_000,
                    engine_health_threshold_ms,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operation_records WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        assert row is not None
        return self._record(row), True

    def get(self, operation_id: str) -> OperationRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_records WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            raise OperationNotFoundError(operation_id)
        return self._record(row)

    def get_by_idempotency_key(self, idempotency_key: str) -> OperationRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_records WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return None if row is None else self._record(row)

    def queued(self) -> tuple[OperationRecord, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_records WHERE state = ? ORDER BY sequence",
                (OperationState.QUEUED.value,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def list(self) -> tuple[OperationRecord, ...]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM operation_records ORDER BY sequence").fetchall()
        return tuple(self._record(row) for row in rows)

    def start(self, operation_id: str, *, now_ns: int | None = None) -> OperationRecord:
        timestamp = time.time_ns() if now_ns is None else now_ns
        return self._transition(
            operation_id,
            expected=(OperationState.QUEUED,),
            target=OperationState.IN_FLIGHT,
            updates={"started_at_ns": timestamp},
        )

    def cancel_before_start(self, operation_id: str, *, now_ns: int | None = None) -> OperationRecord:
        timestamp = time.time_ns() if now_ns is None else now_ns
        return self._transition(
            operation_id,
            expected=(OperationState.QUEUED,),
            target=OperationState.CANCELLED_BEFORE_START,
            updates={"finished_at_ns": timestamp},
        )

    def mark_client_timed_out(self, operation_id: str) -> OperationRecord:
        return self._transition(
            operation_id,
            expected=(OperationState.IN_FLIGHT,),
            target=OperationState.CLIENT_TIMED_OUT,
        )

    def mark_engine_unresponsive(self, operation_id: str) -> OperationRecord:
        return self._transition(
            operation_id,
            expected=(OperationState.IN_FLIGHT, OperationState.CLIENT_TIMED_OUT),
            target=OperationState.ENGINE_UNRESPONSIVE,
        )

    def complete(
        self,
        operation_id: str,
        result: object,
        *,
        now_ns: int | None = None,
    ) -> OperationRecord:
        timestamp = time.time_ns() if now_ns is None else now_ns
        result_json = canonical_json(result, maximum_bytes=self.maximum_json_bytes)
        admitted = (
            OperationState.IN_FLIGHT.value,
            OperationState.CLIENT_TIMED_OUT.value,
            OperationState.ENGINE_UNRESPONSIVE.value,
        )
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE operation_records
                SET state = CASE
                        WHEN state = ? THEN ?
                        WHEN state = ? OR ? >= client_deadline_at_ns THEN ?
                        ELSE ?
                    END,
                    result_json = ?, finished_at_ns = ?, version = version + 1
                WHERE operation_id = ? AND state IN (?, ?, ?)
                """,
                (
                    OperationState.ENGINE_UNRESPONSIVE.value,
                    OperationState.RECONCILIATION_REQUIRED.value,
                    OperationState.CLIENT_TIMED_OUT.value,
                    timestamp,
                    OperationState.COMPLETED_AFTER_CLIENT_TIMEOUT.value,
                    OperationState.COMPLETED.value,
                    result_json,
                    timestamp,
                    operation_id,
                    *admitted,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT state FROM operation_records WHERE operation_id = ?", (operation_id,)
                ).fetchone()
                if row is None:
                    raise OperationNotFoundError(operation_id)
                raise InvalidOperationTransitionError(
                    f"cannot complete operation from {row['state']}"
                )
            row = connection.execute(
                "SELECT * FROM operation_records WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        assert row is not None
        return self._record(row)

    def fail(
        self,
        operation_id: str,
        error: object,
        *,
        now_ns: int | None = None,
    ) -> OperationRecord:
        timestamp = time.time_ns() if now_ns is None else now_ns
        error_json = canonical_json(error, maximum_bytes=self.maximum_json_bytes)
        return self._transition(
            operation_id,
            expected=(
                OperationState.IN_FLIGHT,
                OperationState.CLIENT_TIMED_OUT,
            ),
            target=OperationState.FAILED,
            updates={"error_json": error_json, "finished_at_ns": timestamp},
        )

    def require_reconciliation(
        self,
        operation_id: str,
        error: object,
        *,
        now_ns: int | None = None,
    ) -> OperationRecord:
        timestamp = time.time_ns() if now_ns is None else now_ns
        error_json = canonical_json(error, maximum_bytes=self.maximum_json_bytes)
        return self._transition(
            operation_id,
            expected=(
                OperationState.IN_FLIGHT,
                OperationState.CLIENT_TIMED_OUT,
                OperationState.ENGINE_UNRESPONSIVE,
            ),
            target=OperationState.RECONCILIATION_REQUIRED,
            updates={"error_json": error_json, "finished_at_ns": timestamp},
        )

    def reconcile_orphans(self, *, now_ns: int | None = None) -> int:
        timestamp = time.time_ns() if now_ns is None else now_ns
        states = (
            OperationState.IN_FLIGHT.value,
            OperationState.CLIENT_TIMED_OUT.value,
            OperationState.ENGINE_UNRESPONSIVE.value,
        )
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE operation_records
                SET state = ?, finished_at_ns = ?, version = version + 1
                WHERE state IN (?, ?, ?)
                """,
                (OperationState.RECONCILIATION_REQUIRED.value, timestamp, *states),
            )
            return cursor.rowcount

    def cancel_expired_queued(self, *, now_ns: int | None = None) -> tuple[str, ...]:
        timestamp = time.time_ns() if now_ns is None else now_ns
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT operation_id FROM operation_records
                WHERE state = ? AND queue_deadline_at_ns <= ?
                ORDER BY sequence
                """,
                (OperationState.QUEUED.value, timestamp),
            ).fetchall()
            operation_ids = tuple(str(row[0]) for row in rows)
            if operation_ids:
                placeholders = ",".join("?" for _ in operation_ids)
                connection.execute(
                    f"""
                    UPDATE operation_records
                    SET state = ?, finished_at_ns = ?, version = version + 1
                    WHERE state = ? AND operation_id IN ({placeholders})
                    """,
                    (
                        OperationState.CANCELLED_BEFORE_START.value,
                        timestamp,
                        OperationState.QUEUED.value,
                        *operation_ids,
                    ),
                )
        return operation_ids

    def cancel_all_queued(self, *, now_ns: int | None = None) -> tuple[str, ...]:
        timestamp = time.time_ns() if now_ns is None else now_ns
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT operation_id FROM operation_records WHERE state = ? ORDER BY sequence",
                (OperationState.QUEUED.value,),
            ).fetchall()
            operation_ids = tuple(str(row[0]) for row in rows)
            connection.execute(
                """
                UPDATE operation_records
                SET state = ?, finished_at_ns = ?, version = version + 1
                WHERE state = ?
                """,
                (
                    OperationState.CANCELLED_BEFORE_START.value,
                    timestamp,
                    OperationState.QUEUED.value,
                ),
            )
        return operation_ids

    def _transition(
        self,
        operation_id: str,
        *,
        expected: Iterable[OperationState],
        target: OperationState,
        updates: dict[str, object] | None = None,
    ) -> OperationRecord:
        expected_tuple = tuple(expected)
        if not expected_tuple:
            raise ValueError("expected states cannot be empty")
        updates = {} if updates is None else dict(updates)
        admitted_columns = {"started_at_ns", "finished_at_ns", "result_json", "error_json"}
        if not set(updates).issubset(admitted_columns):
            raise ValueError("transition contains an unsupported update")
        assignments = ["state = ?", "version = version + 1"]
        parameters: list[object] = [target.value]
        for column in sorted(updates):
            assignments.append(f"{column} = ?")
            parameters.append(updates[column])
        placeholders = ",".join("?" for _ in expected_tuple)
        parameters.extend([operation_id, *(state.value for state in expected_tuple)])
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE operation_records SET {', '.join(assignments)}
                WHERE operation_id = ? AND state IN ({placeholders})
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT state FROM operation_records WHERE operation_id = ?", (operation_id,)
                ).fetchone()
                if row is None:
                    raise OperationNotFoundError(operation_id)
                raise InvalidOperationTransitionError(
                    f"cannot transition operation from {row['state']} to {target.value}"
                )
            row = connection.execute(
                "SELECT * FROM operation_records WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        assert row is not None
        return self._record(row)

    @staticmethod
    def _validate_identity(handler_name: str, idempotency_key: str) -> None:
        if not isinstance(handler_name, str) or not _HANDLER_NAME.fullmatch(handler_name):
            raise ValueError("handler_name must be a lowercase named operation")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        if len(idempotency_key.encode("utf-8")) > _MAX_IDEMPOTENCY_KEY_BYTES:
            raise ValueError("idempotency_key is too large")

    @staticmethod
    def _record(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            sequence=int(row["sequence"]),
            operation_id=str(row["operation_id"]),
            idempotency_key=str(row["idempotency_key"]),
            handler_name=str(row["handler_name"]),
            payload=_decode_json(row["payload_json"]),
            state=OperationState(row["state"]),
            admitted_at_ns=int(row["admitted_at_ns"]),
            queue_deadline_at_ns=int(row["queue_deadline_at_ns"]),
            client_deadline_at_ns=int(row["client_deadline_at_ns"]),
            engine_health_threshold_ms=int(row["engine_health_threshold_ms"]),
            started_at_ns=None if row["started_at_ns"] is None else int(row["started_at_ns"]),
            finished_at_ns=None if row["finished_at_ns"] is None else int(row["finished_at_ns"]),
            result=_decode_json(row["result_json"]),
            error=_decode_json(row["error_json"]),
            version=int(row["version"]),
        )


__all__ = [
    "IdempotencyConflictError",
    "InvalidOperationTransitionError",
    "OperationNotFoundError",
    "OperationRecord",
    "OperationState",
    "OperationStore",
    "TERMINAL_STATES",
    "new_idempotency_key",
]
