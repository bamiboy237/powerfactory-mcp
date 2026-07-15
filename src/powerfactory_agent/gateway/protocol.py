"""Narrow protocol implemented by fake and Windows vendor adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class PowerFactoryGateway(Protocol):
    """Vendor primitives only; orchestration and product policy live above it."""

    def start(self, request: SessionStartRequest) -> SessionObservation: ...

    def inspect_context(self) -> ContextObservation: ...

    def activate_context(
        self,
        request: ContextActivationRequest,
    ) -> ContextActivationObservation: ...

    def query_objects(self, request: ObjectQueryRequest) -> ObjectQueryBatch: ...

    def observe_dependencies(self, request: DependencyReadRequest) -> DependencyObservation: ...

    def execute_command(self, request: CommandExecutionRequest) -> CommandExecutionObservation: ...

    def collect_results(self, request: ResultCollectionRequest) -> ResultBatch: ...

    def read_logs(self, request: LogReadRequest) -> LogBatch: ...

    def write_attribute(self, request: AttributeWriteRequest) -> AttributeWriteObservation: ...

    def close(self) -> CleanupObservation: ...


__all__ = ["PowerFactoryGateway"]
