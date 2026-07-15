"""Serialized owner for named, durable, platform-independent gateway operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import queue
import threading
import time

from powerfactory_agent.persistence import (
    InvalidOperationTransitionError,
    OperationRecord,
    OperationState,
    OperationStore,
)
from powerfactory_agent.serialization import canonical_json


OperationHandler = Callable[[object], object]


@dataclass(frozen=True, slots=True)
class OperationRequest:
    handler_name: str
    payload: object
    idempotency_key: str


class WorkerAdmissionError(RuntimeError):
    pass


class WorkerClosedError(WorkerAdmissionError):
    pass


class QueueCapacityError(WorkerAdmissionError):
    pass


class EngineQuarantinedError(WorkerAdmissionError):
    pass


class UnknownOperationHandlerError(ValueError):
    pass


class KnownOperationFailure(RuntimeError):
    """A handler guarantees it failed before causing any external effect."""


class SerializedOperationWorker:
    """Execute handlers FIFO on one stable thread while status remains responsive."""

    def __init__(
        self,
        store: OperationStore,
        handlers: Mapping[str, OperationHandler],
        *,
        max_queue_size: int,
        queue_deadline_ms: int,
        client_response_deadline_ms: int,
        engine_health_threshold_ms: int,
        shutdown_drain_deadline_ms: int = 5_000,
        watchdog_interval_ms: int = 5,
    ) -> None:
        for name, value in {
            "max_queue_size": max_queue_size,
            "queue_deadline_ms": queue_deadline_ms,
            "client_response_deadline_ms": client_response_deadline_ms,
            "engine_health_threshold_ms": engine_health_threshold_ms,
            "shutdown_drain_deadline_ms": shutdown_drain_deadline_ms,
            "watchdog_interval_ms": watchdog_interval_ms,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if not isinstance(handlers, Mapping):
            raise TypeError("handlers must be a mapping")
        handler_registry = dict(handlers)
        if not handler_registry or any(not callable(handler) for handler in handler_registry.values()):
            raise ValueError("handlers must contain at least one callable")

        self.store = store
        self._handlers = handler_registry
        self.max_queue_size = max_queue_size
        self.queue_deadline_ms = queue_deadline_ms
        self.client_response_deadline_ms = client_response_deadline_ms
        self.engine_health_threshold_ms = engine_health_threshold_ms
        self.shutdown_drain_deadline_ms = shutdown_drain_deadline_ms
        self.watchdog_interval_ms = watchdog_interval_ms

        self._queue: queue.Queue[str] = queue.Queue(maxsize=max_queue_size)
        self._admission_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._changed = threading.Condition()
        self._stopping = False
        self._quarantined = False
        self._active_operation_id: str | None = None
        self._owner_thread_id: int | None = None

        # A prior process cannot prove the result of an interrupted call. Queued
        # work is cancelled rather than implicitly replayed across process ownership.
        self.store.reconcile_orphans()
        self.store.cancel_all_queued()

        self._owner_thread = threading.Thread(
            target=self._owner_main,
            name="powerfactory-serialized-owner",
            daemon=True,
        )
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_main,
            name="powerfactory-operation-watchdog",
            daemon=True,
        )
        self._owner_thread.start()
        self._watchdog_thread.start()

    @property
    def owner_thread_id(self) -> int | None:
        with self._state_lock:
            return self._owner_thread_id

    @property
    def active_operation_id(self) -> str | None:
        with self._state_lock:
            return self._active_operation_id

    @property
    def quarantined(self) -> bool:
        with self._state_lock:
            return self._quarantined

    @property
    def stopping(self) -> bool:
        with self._state_lock:
            return self._stopping

    def submit(self, request: OperationRequest, *, wait: bool = True) -> OperationRecord:
        if not isinstance(request, OperationRequest):
            raise TypeError("request must be OperationRequest")
        if request.handler_name not in self._handlers:
            raise UnknownOperationHandlerError(request.handler_name)

        with self._admission_lock:
            existing = self.store.get_by_idempotency_key(request.idempotency_key)
            if existing is None:
                with self._state_lock:
                    if self._stopping:
                        raise WorkerClosedError("serialized owner is stopping")
                    if self._quarantined:
                        raise EngineQuarantinedError("serialized owner is quarantined")
                if self._queue.full():
                    raise QueueCapacityError("serialized owner queue is full")
            record, created = self.store.admit(
                handler_name=request.handler_name,
                payload=request.payload,
                idempotency_key=request.idempotency_key,
                queue_deadline_ms=self.queue_deadline_ms,
                client_response_deadline_ms=self.client_response_deadline_ms,
                engine_health_threshold_ms=self.engine_health_threshold_ms,
            )
            if created:
                self._queue.put_nowait(record.operation_id)
                self._notify_changed()

        if not wait:
            return record
        return self._wait_for_client(record.operation_id)

    def status(self, operation_id: str) -> OperationRecord:
        return self.store.get(operation_id)

    def wait_for_terminal(self, operation_id: str, *, timeout_ms: int) -> OperationRecord:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise TypeError("timeout_ms must be an integer")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        deadline = time.monotonic_ns() + timeout_ms * 1_000_000
        while True:
            record = self.store.get(operation_id)
            if record.terminal:
                return record
            remaining = deadline - time.monotonic_ns()
            if remaining <= 0:
                return record
            with self._changed:
                self._changed.wait(min(remaining / 1_000_000_000, 0.05))

    def close(self, *, timeout_ms: int | None = None) -> bool:
        drain_ms = self.shutdown_drain_deadline_ms if timeout_ms is None else timeout_ms
        if isinstance(drain_ms, bool) or not isinstance(drain_ms, int):
            raise TypeError("timeout_ms must be an integer")
        if drain_ms < 1:
            raise ValueError("timeout_ms must be positive")
        deadline_ns = time.monotonic_ns() + drain_ms * 1_000_000
        with self._admission_lock:
            with self._state_lock:
                self._stopping = True
            self.store.cancel_all_queued()
            self._notify_changed()

        self._owner_thread.join(max(0, deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        if self._owner_thread.is_alive():
            operation_id = self.active_operation_id
            if operation_id is not None:
                try:
                    self.store.mark_engine_unresponsive(operation_id)
                except InvalidOperationTransitionError:
                    pass
            with self._state_lock:
                self._quarantined = True
            self._notify_changed()
            return False
        self._watchdog_thread.join(max(0, deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        if self._watchdog_thread.is_alive():
            with self._state_lock:
                self._quarantined = True
            return False
        return True

    def _wait_for_client(self, operation_id: str) -> OperationRecord:
        while True:
            record = self.store.get(operation_id)
            if record.terminal or record.state in {
                OperationState.CLIENT_TIMED_OUT,
                OperationState.ENGINE_UNRESPONSIVE,
            }:
                return record
            remaining_ns = record.client_deadline_at_ns - time.time_ns()
            if remaining_ns <= 0:
                if record.state is OperationState.IN_FLIGHT:
                    try:
                        return self.store.mark_client_timed_out(operation_id)
                    except InvalidOperationTransitionError:
                        continue
                return record
            with self._changed:
                self._changed.wait(min(remaining_ns / 1_000_000_000, 0.05))

    def _owner_main(self) -> None:
        with self._state_lock:
            self._owner_thread_id = threading.get_ident()
        self._notify_changed()
        while True:
            with self._state_lock:
                if self._stopping and self._queue.empty() and self._active_operation_id is None:
                    return
            try:
                operation_id = self._queue.get(timeout=0.02)
            except queue.Empty:
                continue
            try:
                record = self.store.get(operation_id)
                if record.state is not OperationState.QUEUED:
                    continue
                if time.time_ns() >= record.queue_deadline_at_ns:
                    try:
                        self.store.cancel_before_start(operation_id)
                    except InvalidOperationTransitionError:
                        pass
                    self._notify_changed()
                    continue
                try:
                    record = self.store.start(operation_id)
                except InvalidOperationTransitionError:
                    continue
                with self._state_lock:
                    self._active_operation_id = operation_id
                self._notify_changed()
                self._execute(record)
            finally:
                with self._state_lock:
                    if self._active_operation_id == operation_id:
                        self._active_operation_id = None
                self._queue.task_done()
                self._notify_changed()

    def _execute(self, record: OperationRecord) -> None:
        handler = self._handlers[record.handler_name]
        # Round-trip creates an owned tree of JSON primitives and prevents a
        # caller from mutating the request object while the handler is running.
        owned_payload = json.loads(canonical_json(record.payload))
        try:
            result = handler(owned_payload)
        except KnownOperationFailure as exc:
            error = self._failure_evidence("known_no_effect_failure", exc)
            try:
                self.store.fail(record.operation_id, error)
            except InvalidOperationTransitionError:
                self._reconcile(record.operation_id, error)
            self._notify_changed()
            return
        except Exception as exc:
            self._reconcile(
                record.operation_id,
                self._failure_evidence("uncertain_handler_outcome", exc),
            )
            self._notify_changed()
            return
        try:
            canonical_json(result, maximum_bytes=self.store.maximum_json_bytes)
        except Exception as exc:
            self._reconcile(
                record.operation_id,
                self._failure_evidence("unserializable_handler_outcome", exc),
            )
            self._notify_changed()
            return
        try:
            self.store.complete(record.operation_id, result)
        except InvalidOperationTransitionError:
            pass
        self._notify_changed()

    def _reconcile(self, operation_id: str, error: object) -> None:
        try:
            self.store.require_reconciliation(operation_id, error)
        except InvalidOperationTransitionError:
            pass

    @staticmethod
    def _failure_evidence(category: str, exc: Exception) -> dict[str, str]:
        return {
            "category": category,
            "exception_type": type(exc).__name__,
            "message": "operation outcome requires durable classification",
        }

    def _watchdog_main(self) -> None:
        sleep_seconds = self.watchdog_interval_ms / 1_000
        while True:
            with self._state_lock:
                stopping = self._stopping
                active = self._active_operation_id
            cancelled = self.store.cancel_expired_queued()
            if cancelled:
                self._notify_changed()
            if active is not None:
                try:
                    record = self.store.get(active)
                except LookupError:
                    record = None
                if (
                    record is not None
                    and record.started_at_ns is not None
                    and record.state in {OperationState.IN_FLIGHT, OperationState.CLIENT_TIMED_OUT}
                    and time.time_ns()
                    >= record.started_at_ns + record.engine_health_threshold_ms * 1_000_000
                ):
                    transitioned = False
                    with self._admission_lock:
                        try:
                            self.store.mark_engine_unresponsive(active)
                            transitioned = True
                        except InvalidOperationTransitionError:
                            pass
                        if transitioned:
                            with self._state_lock:
                                self._quarantined = True
                    if transitioned:
                        self._notify_changed()
            if stopping and not self._owner_thread.is_alive():
                return
            time.sleep(sleep_seconds)

    def _notify_changed(self) -> None:
        with self._changed:
            self._changed.notify_all()


__all__ = [
    "EngineQuarantinedError",
    "KnownOperationFailure",
    "OperationHandler",
    "OperationRequest",
    "QueueCapacityError",
    "SerializedOperationWorker",
    "UnknownOperationHandlerError",
    "WorkerAdmissionError",
    "WorkerClosedError",
]
