from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import time
import unittest

from powerfactory_agent.domain import (
    AttributeWriteObservation,
    AttributeWriteRequest,
    CleanupObservation,
    CommandExecutionObservation,
    CommandExecutionRequest,
    CommandSetting,
    ContextActivationObservation,
    ContextActivationRequest,
    ContextObservation,
    DependencyObservation,
    DependencyReadRequest,
    LogBatch,
    LogReadRequest,
    ObjectQueryBatch,
    ObjectQueryRequest,
    ObjectQueryScope,
    OutOfServicePolicy,
    Quantity,
    ResultBatch,
    ResultCollectionRequest,
    SessionObservation,
    SessionStartRequest,
)
from powerfactory_agent.gateway import (
    DeterministicPrimitiveGateway,
    GatewayOwnerHandler,
    SerializedPowerFactoryOwner,
)
from powerfactory_agent.persistence import OperationRecord, OperationState, OperationStore, SQLiteDatabase


class AuditedPrimitiveGateway(DeterministicPrimitiveGateway):
    def __init__(self) -> None:
        super().__init__(allow_test_writes=True)
        self.calls: list[tuple[str, int]] = []

    def _audit(self, name: str) -> None:
        self.calls.append((name, threading.get_ident()))

    def start(self, request):
        self._audit("start")
        return super().start(request)

    def inspect_context(self):
        self._audit("inspect_context")
        return super().inspect_context()

    def activate_context(self, request):
        self._audit("activate_context")
        return super().activate_context(request)

    def query_objects(self, request):
        self._audit("query_objects")
        return super().query_objects(request)

    def observe_dependencies(self, request):
        self._audit("observe_dependencies")
        return super().observe_dependencies(request)

    def execute_command(self, request):
        self._audit("execute_command")
        return super().execute_command(request)

    def collect_results(self, request):
        self._audit("collect_results")
        return super().collect_results(request)

    def read_logs(self, request):
        self._audit("read_logs")
        return super().read_logs(request)

    def write_attribute(self, request):
        self._audit("write_attribute")
        return super().write_attribute(request)

    def close(self):
        self._audit("close")
        return super().close()


class SerializedGatewayOwnerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.gateway = AuditedPrimitiveGateway()
        self.owner = SerializedPowerFactoryOwner(
            self.gateway,
            OperationStore(
                SQLiteDatabase(Path(self.temporary_directory.name) / "owner-operations.db")
            ),
            max_queue_size=32,
            queue_deadline_ms=2_000,
            client_response_deadline_ms=2_000,
            engine_health_threshold_ms=5_000,
            shutdown_drain_deadline_ms=1_000,
            watchdog_interval_ms=2,
        )

    def tearDown(self) -> None:
        self.owner.shutdown_serialization(timeout_ms=1_000)
        self.temporary_directory.cleanup()

    def result(self, record: OperationRecord, result_type):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = self.owner.status(record.operation_id)
            if status.terminal:
                return self.owner.completed_result(record.operation_id, result_type)
            time.sleep(0.005)
        self.fail(f"operation {record.operation_id} did not complete")

    def test_every_primitive_round_trips_on_the_owner_thread(self) -> None:
        self.assertFalse(self.gateway.started)
        self.assertEqual([], self.gateway.calls)

        start_request = SessionStartRequest("fixture", "profile", "2026", "SP0", False)
        start = self.owner.submit_start(start_request, idempotency_key="owner-start")
        session = self.result(start, SessionObservation)
        duplicate = self.owner.submit_start(start_request, idempotency_key="owner-start")
        self.assertEqual(start.operation_id, duplicate.operation_id)
        self.assertEqual(GatewayOwnerHandler.START.value, start.handler_name)

        activation = self.result(
            self.owner.submit_activate_context(
                ContextActivationRequest("project-fixture", "study-fixture", None),
                idempotency_key="owner-activate",
            ),
            ContextActivationObservation,
        )
        context = self.result(
            self.owner.submit_inspect_context(idempotency_key="owner-inspect"),
            ContextObservation,
        )
        self.assertEqual(session.session_id, context.session_id)
        assert activation.context.configuration_key is not None
        configuration_key = activation.context.configuration_key

        query = ObjectQueryRequest(
            configuration_key,
            ObjectQueryScope.ACTIVE_GRIDS,
            OutOfServicePolicy.EXCLUDE,
            (self.gateway.load_class,),
            (self.gateway.active_power_attribute,),
            1,
            None,
        )
        batch = self.result(
            self.owner.submit_query_objects(query, idempotency_key="owner-query"),
            ObjectQueryBatch,
        )
        self.assertEqual(1, len(batch.records))

        dependency = DependencyReadRequest(
            configuration_key,
            (self.gateway.load,),
            (self.gateway.active_power_attribute,),
            (self.gateway.connected_terminal_relationship,),
            1,
        )
        self.result(
            self.owner.submit_observe_dependencies(
                dependency,
                idempotency_key="owner-dependencies",
            ),
            DependencyObservation,
        )

        command_request = CommandExecutionRequest(
            configuration_key,
            self.gateway.load_flow_command,
            (CommandSetting("iopt_net", "balanced"),),
            "owner-command",
        )
        execution = self.result(
            self.owner.submit_execute_command(
                command_request,
                idempotency_key="owner-command",
            ),
            CommandExecutionObservation,
        )
        self.result(
            self.owner.submit_collect_results(
                ResultCollectionRequest(
                    configuration_key,
                    execution.execution_id,
                    (self.gateway.bus,),
                    (self.gateway.bus_voltage_result,),
                    1,
                    None,
                ),
                idempotency_key="owner-results",
            ),
            ResultBatch,
        )
        self.result(
            self.owner.submit_read_logs(
                LogReadRequest(execution.execution_id, 10, 65_536, None),
                idempotency_key="owner-logs",
            ),
            LogBatch,
        )
        self.result(
            self.owner.submit_write_attribute(
                AttributeWriteRequest(
                    configuration_key,
                    self.gateway.load,
                    self.gateway.active_power_attribute,
                    Quantity(Decimal("10"), "MW"),
                    Quantity(Decimal("11"), "MW"),
                    "write-1",
                ),
                idempotency_key="owner-write",
            ),
            AttributeWriteObservation,
        )
        cleanup = self.result(
            self.owner.submit_close(idempotency_key="owner-close"),
            CleanupObservation,
        )
        self.assertTrue(cleanup.cleanup_succeeded)

        expected_calls = {
            "start",
            "inspect_context",
            "activate_context",
            "query_objects",
            "observe_dependencies",
            "execute_command",
            "collect_results",
            "read_logs",
            "write_attribute",
            "close",
        }
        self.assertEqual(expected_calls, {name for name, _ in self.gateway.calls})
        self.assertEqual(1, sum(name == "start" for name, _ in self.gateway.calls))
        self.assertEqual({self.owner.owner_thread_id}, {thread_id for _, thread_id in self.gateway.calls})
        self.assertEqual(OperationState.COMPLETED, self.owner.status(start.operation_id).state)


if __name__ == "__main__":
    unittest.main()
