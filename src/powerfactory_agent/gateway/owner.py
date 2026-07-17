"""Typed durable facade over the serialized PowerFactory gateway owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from powerfactory_agent.domain import (
    AttributeWriteObservation,
    AttributeWriteRequest,
    CleanupObservation,
    CommandExecutionObservation,
    CommandExecutionRequest,
    ContextActivationObservation,
    ContextActivationRequest,
    ContextObservation,
    DependencyObservation,
    DependencyReadRequest,
    LogBatch,
    LogReadRequest,
    ObjectQueryBatch,
    ObjectQueryRequest,
    ResultBatch,
    ResultCollectionRequest,
    SessionObservation,
    SessionStartRequest,
)
from powerfactory_agent.persistence import OperationRecord, OperationState, OperationStore
from powerfactory_agent.serialization import from_primitive, to_primitive

from .protocol import PowerFactoryGateway
from .worker import OperationRequest, SerializedOperationWorker


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


class GatewayOwnerHandler(str, Enum):
    START = "powerfactory.gateway.v1.start"
    INSPECT_CONTEXT = "powerfactory.gateway.v1.inspect-context"
    ACTIVATE_CONTEXT = "powerfactory.gateway.v1.activate-context"
    QUERY_OBJECTS = "powerfactory.gateway.v1.query-objects"
    OBSERVE_DEPENDENCIES = "powerfactory.gateway.v1.observe-dependencies"
    EXECUTE_COMMAND = "powerfactory.gateway.v1.execute-command"
    COLLECT_RESULTS = "powerfactory.gateway.v1.collect-results"
    READ_LOGS = "powerfactory.gateway.v1.read-logs"
    WRITE_ATTRIBUTE = "powerfactory.gateway.v1.write-attribute"
    CLOSE = "powerfactory.gateway.v1.close"


@dataclass(frozen=True, slots=True)
class _OperationSpec(Generic[RequestT, ResultT]):
    handler: GatewayOwnerHandler
    method_name: str
    request_type: type[RequestT] | None
    result_type: type[ResultT]


_START = _OperationSpec(GatewayOwnerHandler.START, "start", SessionStartRequest, SessionObservation)
_INSPECT_CONTEXT = _OperationSpec(
    GatewayOwnerHandler.INSPECT_CONTEXT,
    "inspect_context",
    None,
    ContextObservation,
)
_ACTIVATE_CONTEXT = _OperationSpec(
    GatewayOwnerHandler.ACTIVATE_CONTEXT,
    "activate_context",
    ContextActivationRequest,
    ContextActivationObservation,
)
_QUERY_OBJECTS = _OperationSpec(
    GatewayOwnerHandler.QUERY_OBJECTS,
    "query_objects",
    ObjectQueryRequest,
    ObjectQueryBatch,
)
_OBSERVE_DEPENDENCIES = _OperationSpec(
    GatewayOwnerHandler.OBSERVE_DEPENDENCIES,
    "observe_dependencies",
    DependencyReadRequest,
    DependencyObservation,
)
_EXECUTE_COMMAND = _OperationSpec(
    GatewayOwnerHandler.EXECUTE_COMMAND,
    "execute_command",
    CommandExecutionRequest,
    CommandExecutionObservation,
)
_COLLECT_RESULTS = _OperationSpec(
    GatewayOwnerHandler.COLLECT_RESULTS,
    "collect_results",
    ResultCollectionRequest,
    ResultBatch,
)
_READ_LOGS = _OperationSpec(GatewayOwnerHandler.READ_LOGS, "read_logs", LogReadRequest, LogBatch)
_WRITE_ATTRIBUTE = _OperationSpec(
    GatewayOwnerHandler.WRITE_ATTRIBUTE,
    "write_attribute",
    AttributeWriteRequest,
    AttributeWriteObservation,
)
_CLOSE = _OperationSpec(GatewayOwnerHandler.CLOSE, "close", None, CleanupObservation)
_SPECS = (
    _START,
    _INSPECT_CONTEXT,
    _ACTIVATE_CONTEXT,
    _QUERY_OBJECTS,
    _OBSERVE_DEPENDENCIES,
    _EXECUTE_COMMAND,
    _COLLECT_RESULTS,
    _READ_LOGS,
    _WRITE_ATTRIBUTE,
    _CLOSE,
)


class OperationResultUnavailableError(RuntimeError):
    def __init__(self, record: OperationRecord) -> None:
        super().__init__(
            f"operation {record.operation_id} has no admitted completed result in state {record.state.value}"
        )
        self.record = record


class OperationResultTypeError(TypeError):
    pass


class SerializedPowerFactoryOwner:
    """Non-blocking typed admission and durable result access for gateway primitives."""

    def __init__(
        self,
        gateway: PowerFactoryGateway,
        store: OperationStore,
        *,
        max_queue_size: int,
        queue_deadline_ms: int,
        client_response_deadline_ms: int,
        engine_health_threshold_ms: int,
        shutdown_drain_deadline_ms: int = 5_000,
        watchdog_interval_ms: int = 5,
    ) -> None:
        if not isinstance(gateway, PowerFactoryGateway):
            raise TypeError("gateway must satisfy PowerFactoryGateway")
        self.__gateway = gateway
        self._spec_by_handler = {spec.handler.value: spec for spec in _SPECS}
        handlers = {
            spec.handler.value: self._build_handler(spec)
            for spec in _SPECS
        }
        self._worker = SerializedOperationWorker(
            store,
            handlers,
            max_queue_size=max_queue_size,
            queue_deadline_ms=queue_deadline_ms,
            client_response_deadline_ms=client_response_deadline_ms,
            engine_health_threshold_ms=engine_health_threshold_ms,
            shutdown_drain_deadline_ms=shutdown_drain_deadline_ms,
            watchdog_interval_ms=watchdog_interval_ms,
        )

    @property
    def owner_thread_id(self) -> int | None:
        return self._worker.owner_thread_id

    @property
    def quarantined(self) -> bool:
        return self._worker.quarantined

    def diagnostics(self) -> dict[str, bool | str | None]:
        """Return sanitized owner liveness for operational evidence."""

        return self._worker.diagnostics()

    def submit_start(
        self,
        request: SessionStartRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_START, request, idempotency_key, wait_for_response)

    def submit_inspect_context(
        self,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_INSPECT_CONTEXT, None, idempotency_key, wait_for_response)

    def submit_activate_context(
        self,
        request: ContextActivationRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_ACTIVATE_CONTEXT, request, idempotency_key, wait_for_response)

    def submit_query_objects(
        self,
        request: ObjectQueryRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_QUERY_OBJECTS, request, idempotency_key, wait_for_response)

    def submit_observe_dependencies(
        self,
        request: DependencyReadRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_OBSERVE_DEPENDENCIES, request, idempotency_key, wait_for_response)

    def submit_execute_command(
        self,
        request: CommandExecutionRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        if type(request) is not CommandExecutionRequest:
            raise TypeError("execute_command requires CommandExecutionRequest")
        if request.idempotency_key != idempotency_key:
            raise ValueError("command and durable-operation idempotency keys must match")
        return self._submit(_EXECUTE_COMMAND, request, idempotency_key, wait_for_response)

    def submit_collect_results(
        self,
        request: ResultCollectionRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_COLLECT_RESULTS, request, idempotency_key, wait_for_response)

    def submit_read_logs(
        self,
        request: LogReadRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_READ_LOGS, request, idempotency_key, wait_for_response)

    def submit_write_attribute(
        self,
        request: AttributeWriteRequest,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_WRITE_ATTRIBUTE, request, idempotency_key, wait_for_response)

    def submit_close(
        self,
        *,
        idempotency_key: str,
        wait_for_response: bool = False,
    ) -> OperationRecord:
        return self._submit(_CLOSE, None, idempotency_key, wait_for_response)

    def status(self, operation_id: str) -> OperationRecord:
        return self._worker.status(operation_id)

    def completed_result(self, operation_id: str, result_type: type[ResultT]) -> ResultT:
        if not isinstance(result_type, type):
            raise TypeError("result_type must be a type")
        record = self.status(operation_id)
        if record.state not in {
            OperationState.COMPLETED,
            OperationState.COMPLETED_AFTER_CLIENT_TIMEOUT,
        }:
            raise OperationResultUnavailableError(record)
        spec = self._spec_by_handler.get(record.handler_name)
        if spec is None or spec.result_type is not result_type:
            expected = "unknown" if spec is None else spec.result_type.__name__
            raise OperationResultTypeError(
                f"operation result type is {expected}, not {result_type.__name__}"
            )
        return from_primitive(result_type, record.result)

    def shutdown_serialization(self, *, timeout_ms: int | None = None) -> bool:
        """Stop admission infrastructure without directly closing the gateway session."""

        return self._worker.close(timeout_ms=timeout_ms)

    def _submit(
        self,
        spec: _OperationSpec[RequestT, ResultT],
        request: RequestT | None,
        idempotency_key: str,
        wait_for_response: bool,
    ) -> OperationRecord:
        if not isinstance(wait_for_response, bool):
            raise TypeError("wait_for_response must be a boolean")
        if spec.request_type is None:
            if request is not None:
                raise TypeError(f"{spec.method_name} does not accept a request")
            payload: object = {}
        else:
            if type(request) is not spec.request_type:
                raise TypeError(f"{spec.method_name} requires {spec.request_type.__name__}")
            payload = to_primitive(request)
        return self._worker.submit(
            OperationRequest(spec.handler.value, payload, idempotency_key),
            wait=wait_for_response,
        )

    def _build_handler(self, spec: _OperationSpec[object, object]):
        def handler(payload: object) -> object:
            method = getattr(self.__gateway, spec.method_name)
            if spec.request_type is None:
                if payload != {}:
                    raise TypeError(f"{spec.method_name} owner payload must be empty")
                result = method()
            else:
                request = from_primitive(spec.request_type, payload)
                result = method(request)
            if type(result) is not spec.result_type:
                raise TypeError(
                    f"{spec.method_name} returned {type(result).__name__}, expected {spec.result_type.__name__}"
                )
            return to_primitive(result)

        return handler


__all__ = [
    "GatewayOwnerHandler",
    "OperationResultTypeError",
    "OperationResultUnavailableError",
    "SerializedPowerFactoryOwner",
]
