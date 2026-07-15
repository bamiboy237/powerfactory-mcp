from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest

from powerfactory_agent.domain import ContextObservation, SessionObservation, SessionStartRequest
from powerfactory_agent.gateway import (
    DeterministicPrimitiveGateway,
    OperationResultUnavailableError,
    SerializedPowerFactoryOwner,
)
from powerfactory_agent.persistence import OperationState, OperationStore, SQLiteDatabase


class BlockingInspectGateway(DeterministicPrimitiveGateway):
    def __init__(self) -> None:
        super().__init__()
        self.inspect_entered = threading.Event()
        self.inspect_release = threading.Event()
        self.call_threads: list[int] = []

    def start(self, request):
        self.call_threads.append(threading.get_ident())
        return super().start(request)

    def inspect_context(self):
        self.call_threads.append(threading.get_ident())
        self.inspect_entered.set()
        self.inspect_release.wait(1)
        return super().inspect_context()

    def close(self):
        self.call_threads.append(threading.get_ident())
        return super().close()


def wait_terminal(owner: SerializedPowerFactoryOwner, operation_id: str, timeout: float = 2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = owner.status(operation_id)
        if record.terminal:
            return record
        time.sleep(0.005)
    raise AssertionError("operation did not become terminal")


class SerializedGatewayOwnerIntegrationTests(unittest.TestCase):
    def make_owner(self, directory: str, gateway, *, client_deadline_ms: int = 500):
        return SerializedPowerFactoryOwner(
            gateway,
            OperationStore(SQLiteDatabase(Path(directory) / "operations.db")),
            max_queue_size=8,
            queue_deadline_ms=1_000,
            client_response_deadline_ms=client_deadline_ms,
            engine_health_threshold_ms=1_000,
            shutdown_drain_deadline_ms=1_000,
            watchdog_interval_ms=2,
        )

    def test_client_timeout_is_polled_and_late_typed_result_is_retrievable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = BlockingInspectGateway()
            owner = self.make_owner(directory, gateway, client_deadline_ms=30)
            try:
                start = owner.submit_start(
                    SessionStartRequest("fixture", "profile", "2026", "SP0", True),
                    idempotency_key="start",
                )
                wait_terminal(owner, start.operation_id)
                self.assertIsInstance(
                    owner.completed_result(start.operation_id, SessionObservation),
                    SessionObservation,
                )

                submitted = owner.submit_inspect_context(
                    idempotency_key="slow-inspect",
                    wait_for_response=False,
                )
                self.assertTrue(gateway.inspect_entered.wait(timeout=1))
                timed_out = owner.submit_inspect_context(
                    idempotency_key="slow-inspect",
                    wait_for_response=True,
                )
                self.assertEqual(submitted.operation_id, timed_out.operation_id)
                self.assertEqual(OperationState.CLIENT_TIMED_OUT, timed_out.state)
                with self.assertRaises(OperationResultUnavailableError):
                    owner.completed_result(timed_out.operation_id, ContextObservation)
                gateway.inspect_release.set()
                completed = wait_terminal(owner, timed_out.operation_id)
                self.assertEqual(OperationState.COMPLETED_AFTER_CLIENT_TIMEOUT, completed.state)
                self.assertIsInstance(
                    owner.completed_result(timed_out.operation_id, ContextObservation),
                    ContextObservation,
                )
                self.assertEqual({owner.owner_thread_id}, set(gateway.call_threads))
            finally:
                gateway.inspect_release.set()
                close = owner.submit_close(idempotency_key="close")
                wait_terminal(owner, close.operation_id)
                self.assertTrue(owner.shutdown_serialization(timeout_ms=1_000))

    def test_handler_failure_is_durable_and_requires_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = BlockingInspectGateway()
            gateway.inspect_release.set()
            owner = self.make_owner(directory, gateway)
            try:
                failed = owner.submit_inspect_context(
                    idempotency_key="inspect-before-start",
                    wait_for_response=True,
                )
                self.assertEqual(OperationState.RECONCILIATION_REQUIRED, failed.state)
                self.assertEqual("uncertain_handler_outcome", failed.error["category"])
                self.assertEqual(failed, owner.status(failed.operation_id))
                with self.assertRaises(OperationResultUnavailableError):
                    owner.completed_result(failed.operation_id, ContextObservation)
                self.assertEqual({owner.owner_thread_id}, set(gateway.call_threads))
            finally:
                self.assertTrue(owner.shutdown_serialization(timeout_ms=1_000))


if __name__ == "__main__":
    unittest.main()
